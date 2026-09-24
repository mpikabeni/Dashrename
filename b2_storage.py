# b2_storage.py

import os
from pathlib import Path
from typing import Optional

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    boto3 = None
    BotoCoreError = Exception
    ClientError = Exception


# ============================================================
# BACKBLAZE B2 STORAGE
# CREATED BY NEXA
# ============================================================


class B2Storage:

    def __init__(
        self,
        key_id: Optional[str] = None,
        application_key: Optional[str] = None,
        bucket: Optional[str] = None,
        endpoint: Optional[str] = None,
        region: Optional[str] = None,
    ):

        self.key_id = (
            key_id
            or os.getenv("B2_KEY_ID", "")
        ).strip()

        self.application_key = (
            application_key
            or os.getenv("B2_APPLICATION_KEY", "")
        ).strip()

        self.bucket = (
            bucket
            or os.getenv("B2_BUCKET", "")
        ).strip()

        self.endpoint = (
            endpoint
            or os.getenv("B2_ENDPOINT", "")
        ).strip()

        self.region = (
            region
            or os.getenv(
                "B2_REGION",
                "us-west-002"
            )
        ).strip()

        self.enabled = (
            os.getenv(
                "B2_ENABLED",
                "false"
            ).lower()
            in {
                "1",
                "true",
                "yes",
                "on"
            }
        )

        self.client = None

        if self.enabled:
            self._connect()


    # ========================================================
    # CONNECTION
    # ========================================================

    def _connect(self):

        if not boto3:
            raise RuntimeError(
                "boto3 n'est pas installé."
            )

        missing = []

        if not self.key_id:
            missing.append(
                "B2_KEY_ID"
            )

        if not self.application_key:
            missing.append(
                "B2_APPLICATION_KEY"
            )

        if not self.bucket:
            missing.append(
                "B2_BUCKET"
            )

        if not self.endpoint:
            missing.append(
                "B2_ENDPOINT"
            )

        if missing:
            raise RuntimeError(
                "Configuration B2 incomplète : "
                + ", ".join(missing)
            )

        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.key_id,
            aws_secret_access_key=self.application_key,
            region_name=self.region,
        )


    # ========================================================
    # STATUS
    # ========================================================

    def is_available(self):

        return (
            self.enabled
            and self.client is not None
        )


    # ========================================================
    # UPLOAD
    # ========================================================

    def upload_file(
        self,
        local_path,
        object_name,
        content_type=None,
        metadata=None,
    ):

        if not self.is_available():
            return None

        local_path = str(
            Path(local_path)
        )

        object_name = object_name.lstrip(
            "/"
        )

        extra_args = {}

        if content_type:
            extra_args[
                "ContentType"
            ] = content_type

        if metadata:
            extra_args[
                "Metadata"
            ] = {
                str(k): str(v)
                for k, v in metadata.items()
            }

        try:

            self.client.upload_file(
                local_path,
                self.bucket,
                object_name,
                ExtraArgs=extra_args
                if extra_args
                else None
            )

            return self.object_url(
                object_name
            )

        except (
            ClientError,
            BotoCoreError,
            OSError
        ) as error:

            print(
                "B2 UPLOAD ERROR:",
                repr(error)
            )

            return None


    # ========================================================
    # DOWNLOAD
    # ========================================================

    def download_file(
        self,
        object_name,
        local_path
    ):

        if not self.is_available():
            return False

        object_name = object_name.lstrip(
            "/"
        )

        local_path = Path(
            local_path
        )

        local_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        try:

            self.client.download_file(
                self.bucket,
                object_name,
                str(local_path)
            )

            return True

        except (
            ClientError,
            BotoCoreError,
            OSError
        ) as error:

            print(
                "B2 DOWNLOAD ERROR:",
                repr(error)
            )

            return False


    # ========================================================
    # DELETE
    # ========================================================

    def delete_file(
        self,
        object_name
    ):

        if not self.is_available():
            return False

        object_name = object_name.lstrip(
            "/"
        )

        try:

            self.client.delete_object(
                Bucket=self.bucket,
                Key=object_name
            )

            return True

        except (
            ClientError,
            BotoCoreError
        ) as error:

            print(
                "B2 DELETE ERROR:",
                repr(error)
            )

            return False


    # ========================================================
    # EXISTS
    # ========================================================

    def exists(
        self,
        object_name
    ):

        if not self.is_available():
            return False

        object_name = object_name.lstrip(
            "/"
        )

        try:

            self.client.head_object(
                Bucket=self.bucket,
                Key=object_name
            )

            return True

        except (
            ClientError,
            BotoCoreError
        ):

            return False


    # ========================================================
    # OBJECT URL
    # ========================================================

    def object_url(
        self,
        object_name
    ):

        object_name = object_name.lstrip(
            "/"
        )

        if not self.endpoint:
            return ""

        return (
            self.endpoint.rstrip("/")
            + "/"
            + self.bucket
            + "/"
            + object_name
        )


    # ========================================================
    # SOURCE FILE
    # ========================================================

    def upload_source(
        self,
        local_path,
        filename
    ):

        prefix = os.getenv(
            "B2_SOURCE_PREFIX",
            "dash/source"
        ).strip("/")

        object_name = (
            f"{prefix}/{filename}"
        )

        return self.upload_file(
            local_path,
            object_name
        )


    # ========================================================
    # OUTPUT FILE
    # ========================================================

    def upload_output(
        self,
        local_path,
        filename
    ):

        prefix = os.getenv(
            "B2_OUTPUT_PREFIX",
            "dash/output"
        ).strip("/")

        object_name = (
            f"{prefix}/{filename}"
        )

        return self.upload_file(
            local_path,
            object_name
        )


    # ========================================================
    # THUMBNAIL
    # ========================================================

    def upload_thumbnail(
        self,
        local_path,
        filename
    ):

        prefix = os.getenv(
            "B2_THUMBNAIL_PREFIX",
            "dash/thumbnails"
        ).strip("/")

        object_name = (
            f"{prefix}/{filename}"
        )

        return self.upload_file(
            local_path,
            object_name,
            content_type="image/jpeg"
        )


# ============================================================
# SINGLETON
# ============================================================

try:

    b2 = B2Storage()

except Exception as error:

    print(
        "B2 INITIALIZATION ERROR:",
        repr(error)
    )

    b2 = B2Storage(
        key_id="",
        application_key="",
        bucket="",
        endpoint=""
    )
