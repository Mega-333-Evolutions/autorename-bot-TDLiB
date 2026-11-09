FROM python:3.12-slim

# ffmpeg/ffprobe: metadata + track keep/reorder (stream copy, no re-encoding); curl: fetches the Speedtest CLI below;
# libstdc++6: the one library TDLib (a C++ program, shipped inside the tdjson wheel) needs from the system
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# Ookla's official Speedtest CLI for /speed. Optional: if the download fails the build goes on and /speed
# uses the speedtest-cli Python package (also Ookla) instead.
RUN set -eux; \
    case "$(dpkg --print-architecture)" in amd64) arch=x86_64 ;; arm64) arch=aarch64 ;; *) arch="" ;; esac; \
    if [ -n "$arch" ]; then \
        curl -fsSL --retry 3 --max-time 120 -o /tmp/speedtest.tgz "https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-${arch}.tgz" \
        && tar -xzf /tmp/speedtest.tgz -C /tmp speedtest \
        && install -m 0755 /tmp/speedtest /usr/local/bin/ookla-speedtest \
        || echo "Ookla Speedtest CLI not installed: /speed will use the speedtest-cli package"; \
        rm -f /tmp/speedtest.tgz /tmp/speedtest; \
    fi

# Hugging Face Spaces run Docker containers as user 1000
RUN useradd -m -u 1000 user
USER user

ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860 \
    TEMP_DIR=/tmp/rename \
    TDLIB_DIR=/tmp/tdlib

WORKDIR /home/user/app

COPY --chown=user requirements.txt .
# --prefer-binary: a release that ships only source (and needs a compiler this image does not have) never wins over an older one with wheels
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

COPY --chown=user bot ./bot

EXPOSE 7860
CMD ["python", "-m", "bot"]
