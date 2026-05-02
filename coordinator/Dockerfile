FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .
COPY switches.json .
COPY etherlightd_udp start_etherlightd_udp.sh /app/switch-daemon/

CMD ["python", "coordinator.py", "--config", "switches.json"]
