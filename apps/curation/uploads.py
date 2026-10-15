from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class BoundedUploadHandler(FileUploadHandler):
    """Reject oversized multipart files while streaming, before they fill a temp volume."""

    def receive_data_chunk(self, raw_data, start):
        if start + len(raw_data) > 20 * 1024 * 1024:
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None
