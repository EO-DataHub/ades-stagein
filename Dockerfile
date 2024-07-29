# syntax=docker/dockerfile:1
FROM ghcr.io/terradue/ogc-eo-application-package-hands-on/stage:1.3.2

RUN rm -f /etc/apt/apt.conf.d/docker-clean; \
    echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --upgrade pip

ADD requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install -r requirements.txt

COPY stage.py .

CMD ["python", "stage.py"]