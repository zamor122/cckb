import os
import boto3

def test_e2e_storage():
    endpoint = 'http://localhost:9000'
    bucket_name = 'cckb-data'
    dummy_file = 'dummy.txt'
    downloaded_file = 'downloaded_dummy.txt'
    content = b"This is a dummy file containing some random bytes to test the MinIO storage layer loop."

    # Write dummy file
    with open(dummy_file, 'wb') as f:
        f.write(content)

    try:
        # Initialize boto3 client
        s3_client = boto3.client(
            's3',
            endpoint_url=endpoint,
            aws_access_key_id='minioadmin',
            aws_secret_access_key='minioadmin123',
            region_name='us-east-1'
        )

        # Upload
        print(f"Uploading {dummy_file} to bucket '{bucket_name}'...")
        s3_client.upload_file(dummy_file, bucket_name, dummy_file)

        # Download
        print(f"Downloading {dummy_file} from bucket '{bucket_name}' to {downloaded_file}...")
        s3_client.download_file(bucket_name, dummy_file, downloaded_file)

        # Verify
        with open(downloaded_file, 'rb') as f:
            downloaded_content = f.read()

        assert downloaded_content == content, "Downloaded content does not match original content!"
        print("E2E Storage test passed: files are identical!")

    finally:
        # Cleanup
        if os.path.exists(dummy_file):
            os.remove(dummy_file)
        if os.path.exists(downloaded_file):
            os.remove(downloaded_file)

if __name__ == '__main__':
    test_e2e_storage()
