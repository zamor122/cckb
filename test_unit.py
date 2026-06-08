import boto3
from unittest.mock import patch

def get_s3_client(endpoint_url='http://localhost:9000'):
    """Helper function to instantiate boto3 client for MinIO storage."""
    return boto3.client(
        's3',
        endpoint_url=endpoint_url,
        aws_access_key_id='minioadmin',
        aws_secret_access_key='minioadmin123',
        region_name='us-east-1'
    )

def test_s3_client_endpoint_url():
    """Verify that boto3 client constructor ingests the correct local endpoint_url."""
    with patch('boto3.client') as mock_client:
        get_s3_client()
        mock_client.assert_called_once_with(
            's3',
            endpoint_url='http://localhost:9000',
            aws_access_key_id='minioadmin',
            aws_secret_access_key='minioadmin123',
            region_name='us-east-1'
        )
