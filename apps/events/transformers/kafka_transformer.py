class KafkaTransformer:
    def transform(self, payload: dict) -> dict:
        # return {
        #     "document_id": payload["document_id"],
        #     "metadata": payload.get("metadata", {}),
        #     "blob": payload.get("blob", {}),
        #     "file_path": payload.get("file_path", {}),
        # }
        return payload
