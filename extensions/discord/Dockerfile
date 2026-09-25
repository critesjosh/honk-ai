FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py bot.py ./

RUN useradd --create-home --uid 1000 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "-u", "bot.py"]
