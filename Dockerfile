ARG KATSDPDOCKERBASE_REGISTRY=harbor.sdp.kat.ac.za/dpp

FROM $KATSDPDOCKERBASE_REGISTRY/docker-base-build AS build

# Enable Python 3 venv
ENV PATH="/home/kat/.local/bin:$PATH_PYTHON3" VIRTUAL_ENV="$VIRTUAL_ENV_PYTHON3"

# Install Python dependencies
COPY --chown=kat:kat requirements.txt /tmp/install/requirements.txt
RUN uv pip compile /tmp/install/requirements.txt \
    -c /home/kat/docker-base/base-requirements.lock \
    -o /tmp/install/requirements.lock && \
    uv pip install --no-deps \
    -r /tmp/install/requirements.lock && \
    uv pip check



# Install the current package
COPY --chown=kat:kat . /tmp/install/switch_exporter

RUN cd /tmp/install/switch_exporter && \
    uv pip install --no-deps . && uv pip check
#removed setup.py clean uv will handle this

#######################################################################

FROM $KATSDPDOCKERBASE_REGISTRY/docker-base-runtime
LABEL maintainer=sdpdev+switch_exporter@sarao.ac.za

COPY --chown=kat:kat --from=build /home/kat/ve3 /home/kat/ve3
ENV PATH="/home/kat/.local/bin:$PATH_PYTHON3" VIRTUAL_ENV="$VIRTUAL_ENV_PYTHON3"

EXPOSE 9116
ENTRYPOINT ["/sbin/tini", "--", "switch-exporter"]
