import os
import json
import random
import requests
import subprocess
import tempfile
import time
from github import Github

# Retrieve GitHub Action inputs from environment variables
github_token = os.getenv('INPUT_GITHUB_TOKEN')
model_api_key = os.getenv('INPUT_MODEL_API_KEY')
model_name = os.getenv('INPUT_MODEL_NAME')

# Check if the required inputs are available
if not github_token:
    raise ValueError("GitHub token is required.")
if not model_api_key:
    raise ValueError("Model API key is required.")
if not model_name:
    raise ValueError("Model name is required.")

event_name = os.getenv('GITHUB_EVENT_NAME')

# Retrieve the path to the event payload file
event_path = os.getenv('GITHUB_EVENT_PATH')
if not event_path:
    raise ValueError("Event payload path is not available.")

# Read the event payload from the file
with open(event_path, 'r') as file:
    payload = json.load(file)

# Authenticate with GitHub using the provided token
g = Github(github_token)
# Determine the authenticated user's login
github_user = g.get_user().login
repo_name = os.getenv('GITHUB_REPOSITORY')
repo = g.get_repo(repo_name)

# Define the initial comment options
initial_comment_options = [
    "Would you like me to help with this?",
    "Can I assist you with this issue?",
    "I'm here to help with this issue. Let me know if you need any assistance.",
    "Let me know if you want me to assist you with this issue."
]

def has_duplicate_comment(issue_obj, texts):
    """
    Check if the last comment made by the authenticated user on the issue matches any of the given texts.
    """
    comments = list(issue_obj.get_comments())
    # Iterate in reverse order to locate the latest comment by our authenticated user
    for comment in reversed(comments):
        if comment.user.login == github_user:
            return comment.body.strip() in (text.strip() for text in texts)
    return False

def trim_conversation(messages):
    """
    If the total messages exceed 10, return the first 5 and last 5 messages.
    Otherwise, return the original list.
    """
    if len(messages) > 10:
        return messages[:5] + messages[-5:]
    return messages

# Helper: apply unified diff patch via system 'patch' command
def apply_udiff_patch(original_text, udiff_text):
    """
    Apply the udiff patch to the original_text and return the patched content.
    This uses the system 'patch' command.
    """
    with tempfile.NamedTemporaryFile(mode='w+', delete=False) as orig_file:
        orig_file.write(original_text)
        orig_file.flush()
        orig_filename = orig_file.name

    with tempfile.NamedTemporaryFile(mode='w+', delete=False) as patch_file:
        patch_file.write(udiff_text)
        patch_file.flush()
        patch_filename = patch_file.name

    try:
        subprocess.run(["patch", orig_filename, patch_filename], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with open(orig_filename, 'r') as f:
            patched_content = f.read()
    except subprocess.CalledProcessError as e:
        print("Error applying patch:", e)
        patched_content = original_text
    finally:
        os.remove(orig_filename)
        os.remove(patch_filename)
    return patched_content

# Helper: remove code fences from LLM response
def remove_code_fences(text):
    # Remove triple backticks lines from LLM response
    return "\n".join(line for line in text.splitlines() if line.strip() != "```")

# Handler for new issue event
if event_name == 'issues':
    issue_data = payload['issue']
    issue_obj = repo.get_issue(number=issue_data['number'])
    
    # Check if the last comment is a duplicate to avoid sending multiple identical comments
    if not has_duplicate_comment(issue_obj, initial_comment_options):
        chosen_comment = random.choice(initial_comment_options)
        issue_obj.create_comment(chosen_comment)
        print("Sent initial comment.")
    else:
        print("Duplicate comment detected. Not sending another identical message.")

# Handler for issue_comment events
elif event_name == 'issue_comment':
    comment = payload['comment']
    # Retrieve the issue object
    issue_data = payload.get('issue')
    if not issue_data:
        # Fallback extraction from comment's 'issue_url'
        issue_url = comment.get('issue_url', '')
        issue_number = int(issue_url.split('/')[-1]) if issue_url else None
        if issue_number is None:
            raise ValueError("Could not determine issue number from payload.")
        issue_obj = repo.get_issue(number=issue_number)
    else:
        issue_obj = repo.get_issue(number=issue_data['number'])
    
    if not has_duplicate_comment(issue_obj, initial_comment_options):
        # --- Begin: Fetch issue conversation and prepare messages ---
        conversation = []
        # Include the issue itself (its body)
        issue_body = issue_obj.body or ""
        conversation.append({"role": "user", "content": issue_body})
        
        # Add the default message (simulate what was sent)
        default_message = random.choice(initial_comment_options)
        conversation.append({"role": "assistant", "content": default_message})
        
        # Fetch all comments from the issue and add as user messages (skip automated messages)
        comments = list(issue_obj.get_comments())
        for c in comments:
            if not c.body.strip() in initial_comment_options:
                conversation.append({"role": "user", "content": c.body})
        
        # Trim conversation if there are more than 10 messages
        conversation = trim_conversation(conversation)
        
        # Append instructions message at the end
        instructions = (
            "Instructions:\n"
            "Read the conversation and determine weather the user would like you to help. "
            "Start with <think> include a short thinking about the conversation, end with </think> "
            "and then say either one of \"Yes\" or \"No\".\n"
            "Please do not have a very long thinking part, but keep it short."
        )
        conversation.append({"role": "user", "content": instructions})
        # --- End: Construct messages payload ---

        # --- Begin: Prepare the payload for the external API call ---
        payload_data = {
            "messages": conversation,
            "lora": None,
            "model": "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
            "max_tokens": 1672,
            "stream": True
        }

        headers = {
            "accept": "text/event-stream",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "sec-ch-ua": "\"Not A(Brand\";v=\"8\", \"Chromium\";v=\"132\", \"Brave\";v=\"132\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sec-gpc": "1",
            "Referer": "https://playground.ai.cloudflare.com/"
        }

        url = "https://playground.ai.cloudflare.com/api/inference"

        try:
            response = requests.post(url, headers=headers, json=payload_data, timeout=30, stream=True)
            chunks = []
            for line in response.iter_lines():
                if line:
                    try:
                        data = json.loads(line.decode('utf-8'))
                        chunk = data.get("response", "")
                        chunks.append(chunk)
                    except json.JSONDecodeError:
                        continue
            merged_response = "".join(chunks)
            # Extract the decision after </think>
            think_index = merged_response.find("</think>")
            if think_index != -1:
                final_response = merged_response[think_index+len("</think>"):].strip().lower()
                if "yes" in final_response:
                    decision = True
                elif "no" in final_response:
                    decision = False
                else:
                    decision = None
                if decision:
                    # Explore repository files (excluding dot-files and .mds)
                    def get_repo_files():
                        file_list = []
                        for root, dirs, files in os.walk("."):
                            dirs[:] = [d for d in dirs if not d.startswith('.')]
                            for file in files:
                                if file.startswith('.') or file.endswith('.mds'):
                                    continue
                                file_list.append(os.path.join(root, file))
                        return file_list

                    def build_tree(files):
                        tree = {}
                        for f in files:
                            parts = f.strip(os.sep).split(os.sep)
                            d = tree
                            for part in parts:
                                d = d.setdefault(part, {})
                        def format_tree(d, prefix=""):
                            lines = []
                            keys = sorted(d.keys())
                            for i, key in enumerate(keys):
                                is_last = (i == len(keys) - 1)
                                new_prefix = prefix + ("    " if is_last else "│   ")
                                line = prefix + ("└── " if is_last else "├── ") + key
                                lines.append(line)
                                if d[key]:
                                    lines.extend(format_tree(d[key], new_prefix))
                            return lines
                        return "\n".join(format_tree(tree))
                    
                    def add_line_numbers(text):
                        lines = text.split("\n")
                        return "\n".join(f"{i+1}    {line}" for i, line in enumerate(lines))
                    
                    repo_files = get_repo_files()
                    tree_str = build_tree(repo_files)
                    prompt = f"{tree_str}\nRead this issue then determine which are the necessary files for the task, based on the file tree:\n{issue_obj.body or ''}"
                    
                    # Request to alternate model with file tree and issue details using OpenRouter
                    from openai import OpenAI
                    client = OpenAI(
                        base_url="https://openrouter.ai/api/v1",
                        api_key=model_api_key,
                    )
                    
                    completion = client.chat.completions.create(
                        model="google/gemini-2.0-flash-thinking-exp:free",
                        messages=[
                            {"role": "user", "content": prompt}
                        ]
                    )
                    
                    response_text = completion.choices[0].message.content.strip()
                    try:
                        selected_files = json.loads(response_text)
                    except json.JSONDecodeError:
                        selected_files = []
                    
                    # Lookup the selected files and prepare them for modification
                    files = []
                    for filepath in selected_files:
                        try:
                            with open(filepath, 'r') as f:
                                content = f.read()
                                files.append({"path": filepath, "content": content})
                        except Exception as ex:
                            print(f"Error reading {filepath}: {ex}")
                    
                    # Create a new branch for modifications
                    base_ref = repo.get_git_ref("heads/" + repo.default_branch)
                    new_branch_name = f"llm-updates-{int(time.time())}-{random.randint(1000,9999)}"
                    repo.create_git_ref(ref=f"refs/heads/{new_branch_name}", sha=base_ref.object.sha)
                    print(f"Created branch: {new_branch_name}")
                    
                    # For each file, add line numbers and request modification details
                    for file in files:
                        original_content = file["content"]
                        numbered_content = add_line_numbers(original_content)
                        # Build conversation history for the file modification request (all as role:user)
                        file_messages = []
                        for msg in conversation:
                            file_messages.append({"role": "user", "content": msg["content"]})
                        # Append final instruction based on file length
                        if len(numbered_content) < 15000:
                            final_instruction = (
                                f"{numbered_content}\nModify this file as neccessary. In your final response, give the ENTIRE file incorporating your changes. "
                                "Please do not modify other aspects of the code. Your final response should start with a free form flowing text, explaining your changes, "
                                "followed by a code block with the modified code."
                            )
                        else:
                            final_instruction = (
                                f"{numbered_content}\nModify this file as neccessary. In your final response, give a udiff file incorporating your changes. "
                                "Please create syntactically correct udiff files. Your response should start with a free form flowing text, explaining your changes, "
                                "followed by a code block with the udiff."
                            )
                        file_messages.append({"role": "user", "content": final_instruction})
                        
                        # Request modification for the file using OpenRouter
                        file_completion = client.chat.completions.create(
                            model="google/gemini-2.0-flash-thinking-exp:free",
                            messages=file_messages
                        )
                        llm_response = file_completion.choices[0].message.content.strip()
                        # Remove code fences for commit extended description
                        commit_description = remove_code_fences(llm_response)
                        
                        # Determine final new content for the file
                        if len(numbered_content) < 15000:
                            # LLM response is assumed to be FULL file modification
                            new_content = commit_description
                        else:
                            # LLM response is assumed to be a udiff patch, apply it to the original file content
                            new_content = apply_udiff_patch(original_content, commit_description)
                        
                        # Commit change: update file in the branch, commit title "update {file}"
                        try:
                            git_file = repo.get_contents(file["path"], ref=repo.default_branch)
                            commit_message = f"update {file['path']}"
                            repo.update_file(
                                path=file["path"],
                                message=commit_message,
                                content=new_content,
                                sha=git_file.sha,
                                branch=new_branch_name,
                                committer={"name": github_user, "email": f"{github_user}@users.noreply.github.com"},
                                author={"name": github_user, "email": f"{github_user}@users.noreply.github.com"}
                            )
                            print(f"Committed update for {file['path']} with commit message: {commit_message}")
                            print("Extended commit description (LLM response, code fences removed):")
                            print(commit_description)
                        except Exception as ex:
                            print(f"Error committing changes for {file['path']}: {ex}")
                else:
                    print("Decision from LLM was not affirmative. No modifications will be made.")
        except Exception as e:
            print("Error calling external API:", e)
    else:
        print("Duplicate comment detected. Not sending another identical message.")
    
else:
    print(f"No handler defined for event: {event_name}")