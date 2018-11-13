FROM sdp-docker-registry.kat.ac.za:5000/docker-base-build as build

# Enable Python 3 venv
ENV PATH="$PATH_PYTHON3" VIRTUAL_ENV="$VIRTUAL_ENV_PYTHON3"

# Install Python dependencies
COPY --chown=kat:kat requirements.txt /tmp/install/requirements.txt
RUN install-requirements.py -d ~/docker-base/base-requirements.txt -r /tmp/install/requirements.txt

# Install the current package
COPY --chown=kat:kat . /tmp/install/switch_exporter

RUN cd /tmp/install/switch_exporter && \
    ./setup.py clean && pip install --no-deps . && pip check

#######################################################################

FROM sdp-docker-registry.kat.ac.za:5000/docker-base-runtime

COPY --chown=kat:kat --from=build /home/kat/ve3 /home/kat/ve3
ENV PATH="$PATH_PYTHON3" VIRTUAL_ENV="$VIRTUAL_ENV_PYTHON3"

EXPOSE 9116
ENTRYPOINT ["/sbin/tini", "--", "switch-exporter"]
