"""
같은 영상을 30fps(frame_skip=1)와 5fps(frame_skip=6)로 각각 처리해서
1) 처리 시간이 얼마나 줄어드는지
2) Q2의 두 회피 구간(약 34~48초)이 5fps에서도 같은 위치에 잡히는지
확인한다. 모델은 한 번만 로딩해서 재사용 (콜드스타트 왜곡 방지).

실행:
    python remeasure_fps.py --video ../video/김태현_Q2.mp4 \
        --l2cs_weights ../models/L2CSNet_gaze360.pkl --device cuda
"""
import argparse

from extract_l2cs_warm import extract_l2cs_gaze_warm


def find_dip_windows(frames: list[dict], threshold: float = -0.15) -> list[tuple[float, float]]:
    """yaw가 threshold 밑으로 떨어지는 연속 구간(시작~끝 시각)을 찾는다.

    Q2 영상은 정면(양수~0 근처)이 기본이고 회피 시 크게 떨어지는 패턴이라
    간단한 임계값 기준으로 구간을 뽑는다. (그래프로 봤던 -0.15~-0.35 부근 하강 구간)
    """
    windows = []
    in_dip = False
    start_t = None
    for f in frames:
        if f["yaw"] is None:
            continue
        below = f["yaw"] < threshold
        if below and not in_dip:
            in_dip = True
            start_t = f["timestamp"]
        elif not below and in_dip:
            in_dip = False
            windows.append((start_t, f["timestamp"]))
    if in_dip:
        windows.append((start_t, frames[-1]["timestamp"]))
    return windows


def merge_close_windows(windows: list[tuple[float, float]], gap: float = 2.0) -> list[tuple[float, float]]:
    """짧은 끊김(노이즈)으로 나뉜 구간을 합친다."""
    if not windows:
        return []
    merged = [windows[0]]
    for start, end in windows[1:]:
        if start - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def filter_short_windows(windows: list[tuple[float, float]], min_duration: float = 1.0) -> list[tuple[float, float]]:
    """한두 프레임짜리 노이즈 스파이크를 걸러낸다.

    실제 시선 회피는 사람이 고개/눈을 돌렸다 돌아오는 데 최소 0.5~1초는 걸리므로,
    그보다 훨씬 짧은 구간(단일 프레임 튐)은 회피가 아니라 모델 노이즈로 본다.
    """
    return [(s, e) for s, e in windows if (e - s) >= min_duration]


def main(video_path: str, weights_path: str, device: str):
    print("=== 30fps (frame_skip=1) 처리 중 (모델 최초 로딩 포함) ===")
    result_30fps = extract_l2cs_gaze_warm(video_path, weights_path, device, frame_skip=1)
    print(f"처리 프레임: {result_30fps['processed_frame_count']} / {result_30fps['total_frame_count']}")
    print(f"소요 시간: {result_30fps['elapsed_sec']:.2f}초 "
          f"(영상 길이 {result_30fps['video_duration_sec']:.1f}초 대비 "
          f"{result_30fps['elapsed_sec']/result_30fps['video_duration_sec']:.2f}배)")

    print("\n=== 5fps (frame_skip=6) 처리 중 (모델은 캐시 재사용) ===")
    result_5fps = extract_l2cs_gaze_warm(video_path, weights_path, device, frame_skip=6)
    print(f"처리 프레임: {result_5fps['processed_frame_count']} / {result_5fps['total_frame_count']}")
    print(f"소요 시간: {result_5fps['elapsed_sec']:.2f}초 "
          f"(영상 길이 {result_5fps['video_duration_sec']:.1f}초 대비 "
          f"{result_5fps['elapsed_sec']/result_5fps['video_duration_sec']:.2f}배)")

    speedup = result_30fps['elapsed_sec'] / result_5fps['elapsed_sec']
    print(f"\n=== 속도 비교 ===")
    print(f"5fps가 30fps보다 {speedup:.1f}배 빠름 (이론상 6배 근처가 나와야 정상)")

    print(f"\n=== 회피 구간 탐지 비교 ===")
    windows_30 = filter_short_windows(merge_close_windows(find_dip_windows(result_30fps["frames"])))
    windows_5 = filter_short_windows(merge_close_windows(find_dip_windows(result_5fps["frames"])))
    print(f"30fps에서 잡힌 회피 구간: {windows_30}")
    print(f"5fps에서 잡힌 회피 구간:  {windows_5}")

    print(f"\n=== 판정 ===")
    ok_speed = speedup >= 4.0  # 6배 이론치의 여유 있는 하한
    ok_windows = len(windows_5) == len(windows_30) == 2
    print(f"처리 속도 {'OK' if ok_speed else 'FAIL'} (>=4배)")
    print(f"회피 구간 개수 일치 {'OK' if ok_windows else 'FAIL'} (둘 다 2개여야 함)")

    if ok_speed and ok_windows:
        print("\n5fps 다운샘플링 확정 가능: 속도 개선 + 회피 탐지 유지 둘 다 만족")
    else:
        print("\n조건 미충족 - frame_skip 값이나 threshold 재검토 필요")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--l2cs_weights", required=True)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args()
    main(args.video, args.l2cs_weights, args.device)