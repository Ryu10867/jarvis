"""Enroll the owner's face for Face guard.
Look at the camera; slowly turn your head a little left/right/up/down. Press Q to quit early."""
import sys

SAMPLES = 25


def main(cam_index: int = 0):
    import cv2
    import numpy as np

    from .config import resolve
    from .security.face_engine import OWNER_DB, FaceEngine
    engine = FaceEngine()
    cam = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)
    if not cam.isOpened():
        sys.exit("Could not open the camera.")
    embeddings, frame_no = [], 0
    print("Look at the camera and move your head slightly. Capturing...")
    while len(embeddings) < SAMPLES:
        ok, frame = cam.read()
        if not ok:
            continue
        frame_no += 1
        faces = engine.detect(frame)
        shown = frame.copy()
        if len(faces) == 1:
            x, y, w, h = map(int, faces[0][:4])
            cv2.rectangle(shown, (x, y), (x + w, y + h), (0, 200, 0), 2)
            if frame_no % 4 == 0:        # spread samples over time for pose variety
                embeddings.append(engine.embed(frame, faces[0]))
        msg = f"{len(embeddings)}/{SAMPLES}" if len(faces) == 1 else ("No face" if not faces else "Only ONE person please")
        cv2.putText(shown, msg, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
        cv2.imshow("Jarvis - face enrollment", shown)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cam.release()
    cv2.destroyAllWindows()
    if len(embeddings) < 5:
        sys.exit("Not enough samples captured; try again with better lighting.")
    out = resolve(OWNER_DB)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.save(out, np.array(embeddings))
    print(f"Saved {len(embeddings)} face samples to {out}. Face guard will recognise you now.")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
