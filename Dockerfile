# Base image verified against Docker Hub's API on 2026-10-10:
#   curl -s https://hub.docker.com/v2/repositories/library/python/tags/3.14-slim
#   -> name "3.14-slim", digest sha256:a2b82f3c...729170
# It pins the explicit minor version 3.14 used by the local venv.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Runtime dependency only. psycopg2-binary ships a wheel for this interpreter,
# so no compiler, no apt build tooling and no wheel cache stay in the image.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Only the code the process needs; material, docs, tests and the explorer are
# outside the image.
COPY api ./api

# Never root, and never dependent on `useradd` existing in the slim base: the
# account is created when the tool is there, and the numeric uid works either
# way. HOME is set explicitly because no account lookup (pwd, getpass) is
# guaranteed to resolve for a bare uid; the process writes nothing to disk and
# /tmp is the only world-writable place the image guarantees.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    || echo "useradd is not available in this base: running as a bare numeric uid"
ENV HOME=/tmp
USER 10001:10001

# No secret is baked in: DATABASE_URL and REGISTRO_PEPPER arrive at run time.
EXPOSE 8080
CMD ["python", "-m", "api.adapter"]
