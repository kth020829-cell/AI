"""
질문 음성(TTS 결과)을 S3에 업로드. AI가 직접 올리는 구조.

주의: 실제 버킷/자격증명은 백엔드가 준비해줘야 끝까지 검증 가능하다.
지금은 로직만 완성해두고, 버킷 정보가 오면 환경변수만 채우면 바로 동작한다.
"""
import os
import uuid

import boto3
from botocore.exceptions import ClientError

S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", "ap-northeast-2")
S3_PREFIX = os.environ.get("S3_AUDIO_PREFIX", "tts")

_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=S3_REGION)
    return _s3_client


def upload_audio_to_s3(local_path: str, content_type: str = "audio/mpeg") -> str:
    """
    로컬 mp3 파일을 S3에 올리고 접근 가능한 URL을 돌려준다.

    S3_BUCKET 환경변수가 없으면 설정 미완료로 보고 명확한 에러를 낸다 -
    조용히 실패해서 나중에 원인 찾기 힘든 것보다 낫다.
    """
    if not S3_BUCKET:
        raise RuntimeError(
            "S3_BUCKET 환경변수가 설정되지 않았습니다. "
            "백엔드가 버킷을 준비하면 S3_BUCKET, S3_REGION을 설정하세요."
        )

    key = f"{S3_PREFIX}/{uuid.uuid4().hex}.mp3"
    client = _get_s3_client()

    try:
        client.upload_file(
            local_path, S3_BUCKET, key,
            ExtraArgs={"ContentType": content_type},
        )
    except ClientError as e:
        raise RuntimeError(f"S3 업로드 실패: {e}") from e

    return f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{key}"
