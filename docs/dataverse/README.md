# Dataverse setup for Elvira (IP-034)

Operator steps for a Dataverse 6.4+ installation that Elvira reads from and writes to. The dev Dataverse is being
redeployed; none of these steps has been run against it yet. Do them on dev first. Each step names what is
unverified.

`$DV` is the Dataverse base URL. `$API_TOKEN` is a superuser's API token, used only for this setup.

## 1. Load the `elvira_book` metadata block

```bash
curl -H "X-Dataverse-key: $API_TOKEN" -X POST -H "Content-type: text/tab-separated-values" \
  "$DV/api/admin/datasetfield/load" --upload-file docs/dataverse/elvira_book.tsv
```

The block holds these fields:

- `isbn` and `udc`, both repeatable;
- `edition`, `seriesTitle`, `seriesNumber` and `pageCount`;
- `elviraEntryId`, `elviraUrl` and `lending`, which only Elvira writes.

`udc` is free text, not a controlled vocabulary, because the vocabulary grows with the catalog (Q3). The
pre-publish check below validates it instead.

Dataverse requires field names to be unique across all blocks. None of these names clashes with the stock blocks
of 6.x as far as we know. This is unverified on the redeployed instance.

## 2. Update the Solr schema

The new fields must be indexed. Run the guides' `update-fields.sh` against the Solr `schema.xml`, then reload
Solr:

```bash
curl "$DV/api/admin/index/solr/schema" | ./update-fields.sh /usr/local/solr/server/solr/collection1/conf/schema.xml
curl "http://localhost:8983/solr/admin/cores?action=RELOAD&core=collection1"
```

## 3. Enable the block and create the `book` dataset type

Enable the block on the Elvira collection(s):

```bash
curl -H "X-Dataverse-key: $API_TOKEN" -X POST -H "Content-type: application/json" \
  "$DV/api/dataverses/<alias>/metadatablocks" -d '["citation","elvira_book"]'
```

Then create the `book` dataset type and link it to the blocks:

```bash
curl -H "X-Dataverse-key: $API_TOKEN" -X POST -H "Content-Type: application/json" \
  "$DV/api/datasets/datasetTypes" -d '{"name": "book"}'
curl -H "X-Dataverse-key: $API_TOKEN" -X PUT -H "Content-Type: application/json" \
  "$DV/api/datasets/datasetTypes/book" -d '["citation","elvira_book"]'
```

Allow the type in the Elvira collection(s): `PUT $DV/api/dataverses/<alias>/allowedDatasetTypes` with `["book"]`.
Dataset types arrived in Dataverse 6.4, and linking them to metadata blocks in 6.5+. Check the exact endpoints
against the installed version's guide.

## 4. Workflows

Both workflows call Elvira with an `http/sr` step. That step sends a body and compares the response with
`expectedResponse`. Replace `<secret>` with `DATAVERSE_WORKFLOW_SECRET` from Elvira's environment, and
`<elvira>` with Elvira's internal base URL.

### Pre-publish validation (Q9)

```json
{
  "name": "Elvira pre-publish check",
  "steps": [
    {
      "provider": ":internal",
      "stepType": "http/sr",
      "parameters": {
        "url": "<elvira>/api/v1/dataverse-validate",
        "method": "POST",
        "contentType": "application/json",
        "body": "{\"secret\": \"<secret>\", \"dataset_id\": \"${dataset.id}\", \"global_id\": \"${dataset.globalId}\", \"invocation_id\": \"${invocationId}\"}",
        "expectedResponse": "OK.*"
      }
    }
  ]
}
```

Elvira handles book datasets as follows:

- **Answer `OK`:** every ISBN (`elvira_book.isbn` and every `otherId` with agency `ISBN`) has a valid checksum,
  and at least one `udc` value is present.
- **Answer `FAILED: <reason>`:** anything else, with HTTP status 200. The step then fails and stops the publish.
- **Unreadable dataset:** if Elvira cannot read the dataset, for example because the API token is missing or
  Dataverse returns an error, the answer is also `FAILED` (fail closed).
- **Other dataset types:** always `OK`.

Elvira logs every refusal. Whether Dataverse shows the reason to the librarian is unverified.

Register the workflow and make it the `PrePublishDataset` default:

```bash
curl -H "X-Dataverse-key: $API_TOKEN" -X POST -H "Content-type: application/json" \
  "$DV/api/admin/workflows" --upload-file validate-workflow.json
curl -H "X-Dataverse-key: $API_TOKEN" -X PUT -d <workflow id> "$DV/api/admin/workflows/default/PrePublishDataset"
```

### Post-publish import (Q4)

This is the existing `POST <elvira>/api/v1/dataverse-prepublish` step, with the same body. It must be the
`PostPublishDataset` default: Elvira reads only `:latest-published`, so a pre-publish trigger would find no
published version and answer 502.

With an enabled harvest source of adapter `dataverse` (step 8), the callback goes through the generic two-way
sync: Elvira fetches the published version, matches it, merges it per field and logs conflicts. Without such a
source, the older one-way import still runs.

## 5. IP whitelist

Dataverse accepts workflow resume calls only from whitelisted addresses. Add the catalog's egress IP:

```bash
curl -H "X-Dataverse-key: $API_TOKEN" -X PUT -d "127.0.0.1;::1;<catalog egress IP>" \
  "$DV/api/admin/workflows/ip-whitelist"
```

`DATAVERSE_AUTO_WHITELIST_WORKFLOW_RESUME=1` makes Elvira add its own address. It is off by default.

## 6. External tool "Open in Elvira"

Edit `external-tools/open-in-elvira.json`: replace the placeholder host `https://elvira.example.org` with Elvira's
public base URL (`EVILFLOWERS_BASE_URL`). Then register the tool:

```bash
curl -H "X-Dataverse-key: $API_TOKEN" -X POST -H "Content-type: application/json" \
  "$DV/api/admin/externalTools" --upload-file docs/dataverse/external-tools/open-in-elvira.json
```

The tool opens `/resolve/dataverse?id=<dataset PID>`, the generic resolver of the harvest source with slug
`dataverse`. It redirects to the linked entry in the portal, or answers 404.

## 7. Tokens

| Variable | Used for | Rights |
|---|---|---|
| `DATAVERSE_API_TOKEN` | reads (prepublish, validate, harvest runs) | read on the Elvira collections, drafts included (validate reads the draft) |
| `EVILFLOWERS_DATAVERSE_SUPERUSER_TOKEN` | writes (`editMetadata`, `publish?type=updatecurrent`, dataset creation) | superuser; environment only, never the settings store (Q5) |
| `EVILFLOWERS_DATAVERSE_CONTACT_EMAIL` (or `EVILFLOWERS_CONTACT_EMAIL`) | `datasetContact` of datasets Elvira creates | — |
| `DATAVERSE_WORKFLOW_SECRET` | the `secret` in both workflow bodies | shared secret |

If the superuser token is empty, the connector is read-only and Elvira writes nothing to Dataverse.

## 8. The harvest source

The two-way sync is a harvest source with the `dataverse` connector. Create it as a superuser:

```bash
curl -H "Authorization: Bearer $ELVIRA_TOKEN" -H "Content-Type: application/json" -X POST \
  "<elvira>/api/v1/harvest/sources" -d '{
    "slug": "dataverse", "name": "Dataverse", "adapter": "dataverse", "mode": "sync",
    "system_key": "dataverse_pid", "target_catalog_id": "<catalog uuid>", "oai_reexpose": false,
    "config": {"collection": "<alias>"}, "schedule": "*/15 * * * *", "enabled": true
  }'
```

- **The slug must be `dataverse`.** Entries imported before IP-034 have `origin = harvest:dataverse`, and the
  external tool resolves `/resolve/dataverse`.
- **`config`:**
  - `collection` (required): the collection that new datasets go to.
  - `subtree` (optional): the collection a run lists. It defaults to `collection`.
  - `check_batch` (default 50): linked datasets whose existence each incremental run checks. A full run
    checks all of them.
  - `max_withdrawals` (default 20): the most deletions one run reports. This protects against a Dataverse that
    was redeployed empty.
- **Each run:**
  - Lists the datasets of the collection, newest change first, down to the cursor. The cursor is
    `<newest search date>|<check offset>`.
  - Fetches each dataset's `:latest-published` version.
  - Checks a rotating batch of linked datasets with `versions?includeDeaccessioned=true`. A dataset that is
    deaccessioned, or that answers 404, withdraws its entry (Q6). A 5xx or authentication error never does.
- **Entries without a dataset:** entries of the target catalog that have no dataset get a `book` dataset in
  `collection`, published as a major version. Datasets get no files (Q8).
- **Edits:** an edit in Elvira is written with `editMetadata`, with only the changed fields, and then
  `publish?type=updatecurrent`.
- **Open drafts:** if a draft is open in Dataverse, the push fails until someone publishes or discards that
  draft.
