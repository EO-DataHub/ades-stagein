# syntax=docker/dockerfile:1
FROM ghcr.io/terradue/ogc-eo-application-package-hands-on/stage:1.3.2

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY stage.py .

CMD ["python", "stage.py"]