"""
프레임별 시선 좌표(yaw/pitch)를 구간 지표로 변환한다.

입력: [{"frame_idx": int, "timestamp": float, "yaw": float|None, "pitch": float|None}, ...]
출력: 응시 유지 비율, 회피 빈도, 각 회피 구간의 시작·종료 시각(감점 근거용)

설계 원칙
    드롭아웃(모델이 얼굴을 못 찾은 것)과 회피(실제로 시선을 뗀 것)는 다르다.
    드롭아웃은 모델 실패지 사용자 잘못이 아니므로, 응시유지비율/회피빈도 계산에서
    제외하고 별도의 데이터 품질 지표(dropout_rate)로만 남긴다.

    회피 판정은 절대 임계값(yaw_threshold) 기준이다. 이 값은 Q2 촬영 영상으로
    캘리브레이션한 값(-0.15)을 기본값으로 쓰지만, 카메라 각도나 개인차에 따라
    달라질 수 있으므로 실제 서비스에서는 Q1(정면 유지 구간) 평균을 baseline으로
    잡아 상대값으로 판정하는 방식으로 개선하는 게 이상적이다. (TODO로 남겨둠)
"""
from dataclasses import dataclass, field


@dataclass
class AversionEvent:
    """회피 구간 하나. 리포트의 감점 근거(Evidence)로 바로 쓸 수 있는 형태."""
    t_start: float
    t_end: float

    @property
    def duration(self) -> float:
        return round(self.t_end - self.t_start, 2)

    def to_evidence_dict(self, question_id: str = "") -> dict:
        """report_dummy.py의 Evidence 스키마와 맞춘 형태."""
        return {
            "question_id": question_id,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "kind": "weakness",
            "label": "시선 회피",
            "comment": f"{self.t_start}초~{self.t_end}초 구간에서 화면 밖을 응시했습니다.",
        }


@dataclass
class GazeMetrics:
    gaze_maintain_ratio: float          # 0~1. 인식 성공 시간 중 회피가 아니었던 비율
    aversion_frequency: int             # 회피 발생 횟수
    aversion_events: list[AversionEvent]
    dropout_rate: float                 # 데이터 품질 지표. 채점에는 안 씀
    total_duration_sec: float
    valid_duration_sec: float           # 드롭아웃 제외한 유효 인식 시간


def _detect_raw_dips(frames: list[dict], yaw_threshold: float) -> list[tuple[float, float]]:
    """yaw가 threshold 밑으로 떨어지는 연속 구간(시작~끝)을 찾는다. 드롭아웃 프레임은 건너뛴다."""
    windows = []
    in_dip = False
    start_t = None
    last_valid_t = None

    for f in frames:
        if f["yaw"] is None:
            continue  # 드롭아웃은 판정에서 제외
        t = f["timestamp"]
        below = f["yaw"] < yaw_threshold

        if below and not in_dip:
            in_dip = True
            start_t = t
        elif not below and in_dip:
            in_dip = False
            windows.append((start_t, t))

        last_valid_t = t

    if in_dip and last_valid_t is not None:
        windows.append((start_t, last_valid_t))

    return windows


def _merge_close(windows: list[tuple[float, float]], gap: float) -> list[tuple[float, float]]:
    if not windows:
        return []
    merged = [windows[0]]
    for start, end in windows[1:]:
        if start - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _filter_short(windows: list[tuple[float, float]], min_duration: float) -> list[tuple[float, float]]:
    """한두 프레임짜리 노이즈 스파이크 제거. 사람이 고개를 돌렸다 돌아오는 덴 최소 시간이 걸린다."""
    return [(s, e) for s, e in windows if (e - s) >= min_duration]


def compute_gaze_metrics(
    frames: list[dict],
    total_duration_sec: float,
    yaw_threshold: float = -0.15,
    min_aversion_duration: float = 1.0,
    merge_gap_sec: float = 2.0,
) -> GazeMetrics:
    """
    frames: extract_l2cs_gaze_warm() 등이 반환하는 프레임 리스트 (5fps 다운샘플링된 것 권장)
    total_duration_sec: 답변 전체 길이 (영상 길이와 동일)
    """
    if not frames:
        return GazeMetrics(
            gaze_maintain_ratio=0.0, aversion_frequency=0, aversion_events=[],
            dropout_rate=1.0, total_duration_sec=total_duration_sec, valid_duration_sec=0.0,
        )

    total_frames = len(frames)
    dropout_frames = sum(1 for f in frames if f["yaw"] is None)
    dropout_rate = round(dropout_frames / total_frames, 3)

    raw_windows = _detect_raw_dips(frames, yaw_threshold)
    merged = _merge_close(raw_windows, merge_gap_sec)
    filtered = _filter_short(merged, min_aversion_duration)

    events = [AversionEvent(t_start=round(s, 2), t_end=round(e, 2)) for s, e in filtered]

    # 응시유지비율은 "인식이 됐던 시간" 대비 계산 (드롭아웃 시간은 분모에서도 제외)
    valid_frame_ratio = 1 - (dropout_frames / total_frames)
    valid_duration_sec = round(total_duration_sec * valid_frame_ratio, 2)

    aversion_time = sum(e.duration for e in events)
    if valid_duration_sec > 0:
        gaze_maintain_ratio = max(0.0, round(1 - (aversion_time / valid_duration_sec), 3))
    else:
        gaze_maintain_ratio = 0.0  # 인식된 시간이 아예 없으면 판정 불가 -> 최소값

    return GazeMetrics(
        gaze_maintain_ratio=gaze_maintain_ratio,
        aversion_frequency=len(events),
        aversion_events=events,
        dropout_rate=dropout_rate,
        total_duration_sec=total_duration_sec,
        valid_duration_sec=valid_duration_sec,
    )
