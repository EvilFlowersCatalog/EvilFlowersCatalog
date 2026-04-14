FROM alpine:3.20
RUN apk add --no-cache bash curl python3
WORKDIR /opt/dv-workflows

# Copy the Dataverse bootstrap script and workflow definition.
COPY dataverse/scripts/hook.sh ./hook.sh
COPY dataverse/hooks/prepublish-sync.json ./prepublish-sync.json
COPY dataverse/scripts/bootstrap-workflow.sh ./bootstrap-workflow.sh

RUN chmod +x /opt/dv-workflows/hook.sh /opt/dv-workflows/bootstrap-workflow.sh