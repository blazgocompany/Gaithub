FROM python:3.9-slim
WORKDIR /app
COPY main.py .
RUN pip install requests PyGithub openai
CMD ["python", "main.py"]
