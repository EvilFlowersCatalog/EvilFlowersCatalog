FROM alpine:3.20
RUN apk add --no-cache bash curl python3
WORKDIR /opt/dv-workflows

# adjust these paths to where hook + json live in EvilFlowersCatalog repo
COPY dataverse/scripts/hook.sh ./hook.sh
COPY dataverse/scripts/*.json ./

RUN chmod +x /opt/dv-workflows/hook.sh
