"""Face detection (YuNet) + recognition (SFace) using models built into OpenCV - no dlib needed."""
import numpy as np
import cv2

from ..config import resolve

DETECTOR = "models/face_detection_yunet_2023mar.onnx"
RECOGNIZER = "models/face_recognition_sface_2021dec.onnx"
OWNER_DB = "data/owner_faces.npy"


class FaceEngine:
    def __init__(self):
        self.detector = cv2.FaceDetectorYN.create(str(resolve(DETECTOR)), "", (320, 320), 0.85, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(resolve(RECOGNIZER)), "")
        self.owner = self.load_owner()

    @staticmethod
    def load_owner():
        p = resolve(OWNER_DB)
        return np.load(p) if p.exists() else None

    def detect(self, frame):
        h, w = frame.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(frame)
        return [] if faces is None else list(faces)

    def embed(self, frame, face):
        aligned = self.recognizer.alignCrop(frame, face)
        return self.recognizer.feature(aligned).flatten()

    def best_match(self, emb) -> float:
        if self.owner is None:
            return 0.0
        e = emb / np.linalg.norm(emb)
        o = self.owner / np.linalg.norm(self.owner, axis=1, keepdims=True)
        return float(np.max(o @ e))
