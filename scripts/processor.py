
import numpy as np
from annotator.util import resize_image, HWC3


model_canny = None


def canny(img, res=512, l=100, h=200):
    img = resize_image(HWC3(img), res)
    global model_canny
    if model_canny is None:
        from annotator.canny import apply_canny
        model_canny = apply_canny
    result = model_canny(img, l, h)
    return result

def simple_scribble(img, res=512):
    img = resize_image(HWC3(img), res)
    result = np.zeros_like(img, dtype=np.uint8)
    result[np.min(img, axis=2) < 127] = 255
    return result


model_hed = None


def hed(img, res=512):
    img = resize_image(HWC3(img), res)
    global model_hed
    if model_hed is None:
        from annotator.hed import apply_hed
        model_hed = apply_hed
    result = model_hed(img)
    return result

def unload_hed():
    global model_hed
    if model_hed is not None:
        from annotator.hed import unload_hed_model
        unload_hed_model()

def fake_scribble(img, res=512):
    result = hed(img, res)
    import cv2
    from annotator.hed import nms
    result = nms(result, 127, 3.0)
    result = cv2.GaussianBlur(result, (0, 0), 3.0)
    result[result > 10] = 255
    result[result < 255] = 0
    return result


model_mlsd = None


def mlsd(img, res=512, thr_v=0.1, thr_d=0.1):
    img = resize_image(HWC3(img), res)
    global model_mlsd
    if model_mlsd is None:
        from annotator.mlsd import apply_mlsd
        model_mlsd = apply_mlsd
    result = model_mlsd(img, thr_v, thr_d)
    return result

def unload_mlsd():
    global model_mlsd
    if model_mlsd is not None:
        from annotator.mlsd import unload_mlsd_model
        unload_mlsd_model()


model_midas = None


def midas(img, res=512, a=np.pi * 2.0):
    img = resize_image(HWC3(img), res)
    global model_midas
    if model_midas is None:
        from annotator.midas import apply_midas
        model_midas = apply_midas
    results, _ = model_midas(img, a)
    return results

def midas_normal(img, res=512, a=np.pi * 2.0, bg_th=0.4):
    img = resize_image(HWC3(img), res)
    global model_midas
    if model_midas is None:
        from annotator.midas import apply_midas
        model_midas = apply_midas
    _, results  = model_midas(img, a, bg_th)
    return results

def unload_midas():
    global model_midas
    if model_midas is not None:
        from annotator.midas import unload_midas_model
        unload_midas_model()


model_openpose = None


def openpose(img, res=512, has_hand=False):
    img = resize_image(HWC3(img), res)
    global model_openpose
    if model_openpose is None:
        from annotator.openpose import apply_openpose
        model_openpose = apply_openpose
    result, _ = model_openpose(img, has_hand)
    return result

def openpose_hand(img, res=512, has_hand=True):
    img = resize_image(HWC3(img), res)
    global model_openpose
    if model_openpose is None:
        from annotator.openpose import apply_openpose
        model_openpose = apply_openpose
    result, _ = model_openpose(img, has_hand)
    return result

def unload_openpose():
    global model_openpose
    if model_openpose is not None:
        from annotator.openpose import unload_openpose_model
        unload_openpose_model()


model_uniformer = None


def uniformer(img, res=512):
    img = resize_image(HWC3(img), res)
    global model_uniformer
    if model_uniformer is None:
        from annotator.uniformer import apply_uniformer
        model_uniformer = apply_uniformer
    result = model_uniformer(img)
    return result

def unload_uniformer():
    global model_uniformer
    if model_uniformer is not None:
        from annotator.uniformer import unload_uniformer_model
        unload_uniformer_model()