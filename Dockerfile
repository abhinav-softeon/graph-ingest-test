# Python 3.13 for broad wheel availability (pyarrow/streamlit can lag on a
# brand-new Python). Bump to 3.14 only if every dep has 3.14 wheels.
FROM python:3.13-slim

# build-essential: some tree-sitter grammar wheels build from source.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Optional: compile resolver.py/pipeline.py (resolve+derive, ~80% of ingest
# wall time per runs/*.json) to native extensions with Cython, instead of
# running them interpreted. Same algorithm/memory, just faster — see
# setup_cython.py's docstring. Off by default (build is extra time + a
# cython install); the pure-Python .py files always work as a fallback.
# Enable with: docker build --build-arg ENABLE_COMPILED_HOTPATH=true .
ARG ENABLE_COMPILED_HOTPATH=false
RUN if [ "$ENABLE_COMPILED_HOTPATH" = "true" ]; then \
        pip install --no-cache-dir cython setuptools wheel && \
        python setup_cython.py build_ext --inplace ; \
    fi

# Edge spill goes on a fast named volume (see docker-compose.yml). Needs ~10GB
# free for a 130M-edge graph.
ENV GRAPH_EDGE_SPILL_DIR=/data/edge_spill \
    PYTHONUNBUFFERED=1

EXPOSE 8501

CMD ["streamlit", "run", "ui/app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", \
     "--server.headless=true", "--server.maxUploadSize=5000"]
