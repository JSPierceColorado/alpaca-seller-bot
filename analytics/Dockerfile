FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY tests ./tests
ENV PYTHONPATH=/app
RUN python -m compileall -q . && python -m unittest discover -s tests -v
CMD ["python","-u","main.py"]
