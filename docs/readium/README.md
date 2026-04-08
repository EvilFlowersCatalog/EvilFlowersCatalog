# Readium LCP Integration

EvilFlowers Catalog implements **Standard LCP** (Licensed Content Protection) for DRM-protected publication distribution. This directory contains reference documentation for the Readium LCP integration.

## Documentation Index

| Document | Description |
|----------|-------------|
| [Integration Guide](../catalog-wiki/Readium-LCP-Integration.md) | Architecture, services, workflows, API reference, and database models |
| [Client Integration](../catalog-wiki/Readium-LCP-Client-Integration.md) | Frontend/mobile integration with code examples (JS, HTML) |
| [Frontend Integration (TypeScript)](./frontend-integration-guide.md) | Detailed TypeScript API client, React components, and borrowing service |
| [Deployment Guide](../catalog-wiki/Readium-LCP-Deployment.md) | Docker Compose setup, shared filesystem, LCP server configuration |
| [LCP Server Wiki](./lcp-server-wiki/) | Upstream Readium LCP Server documentation (API, encryption tool, deployment) |

## Quick Overview

### How It Works

1. **Admin enables Readium** on an Entry (`readium_enabled=True`)
2. **Content is encrypted** automatically via `lcpencrypt` worker (EPUB/PDF -> AES-256-CBC)
3. **User sets passphrase** on their profile (one-time setup)
4. **User creates a license** via `POST /readium/v1/licenses`
5. **User downloads `.lcpl` file** via `GET /readium/v1/licenses/{id}.lcpl`
6. **User opens in reading app** (Thorium Reader, Aldiko Next) and enters passphrase

### Key Components

- **ContentEncryptionService** - Triggers encryption, tracks status
- **LCPServerClient** - Communicates with LCP License Server (generates/fetches licenses)
- **StatusServerClient** - Communicates with LCP Status Server (device tracking, returns, revocations)
- **LicenseService** - High-level license lifecycle (availability, creation, renewal, return, revocation)

### API Endpoints

All under `/readium/v1/`:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/licenses` | Create new license |
| GET | `/licenses` | List user's licenses |
| GET | `/licenses/{id}` | License details |
| PUT | `/licenses/{id}` | Update state (return, renew, revoke, cancel) |
| GET | `/licenses/{id}.lcpl` | Download license file (License Gateway) |
| GET | `/content/{lcp_content_id}` | Download encrypted publication |
| GET | `/entries/{id}/availability` | Availability calendar |
| GET/POST | `/entries/{id}/encryption` | Encryption status / manual trigger |
| POST | `/hooks/encryption` | Webhook from lcpencrypt worker |
