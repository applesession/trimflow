import os

import boto3
from botocore.config import Config


def upload_file_to_s3(local_path, s3_key):
    s3 = boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_ENDPOINT"),
        aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY"),
        region_name=os.getenv("S3_REGION"),
        config=Config(
            connect_timeout=60,
            read_timeout=3600,
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )

    bucket = os.getenv("S3_BUCKET_NAME")

    print(f"[S3 UPLOAD] {local_path} -> s3://{bucket}/{s3_key}")
    s3.upload_file(str(local_path), bucket, s3_key)
