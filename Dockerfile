FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./
COPY hwm_tracker.py ./
COPY tests ./tests
RUN python -m compileall -q main.py hwm_tracker.py && python -m unittest discover -s tests -v
# Persistent Railway worker; internal market-hour scheduler, no Railway Cron.
CMD ["python","-u","main.py"]
