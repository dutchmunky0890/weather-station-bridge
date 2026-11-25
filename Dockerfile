FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir paho-mqtt==2.1.0 dnspython==2.7.0

COPY receiver.py /app/receiver.py

CMD ["python3", "/app/receiver.py"]
