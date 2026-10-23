FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

RUN useradd --create-home lawyerbot && mkdir -p /app/data && chown lawyerbot /app/data
USER lawyerbot

ENV WEB_HOST=0.0.0.0
EXPOSE 8000
CMD ["python", "-m", "app.main"]
