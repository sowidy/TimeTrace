"""安卓应用内更新：版本查询 + APK 下载（1.9.8 起）。

公开接口（不鉴权）：版本号和下载地址不是用户数据，任何人都能查。
版本信息存在 server/app-version.json（发版时随 APK 一起更新），APK 文件
存在 server/static/ 下。客户端拿 version_code 和自己已装的比。
"""
import json
import logging
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

log = logging.getLogger("pctime")

router = APIRouter()

# server/ 目录：app-version.json 和 static/ 都放在这里。
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(SERVER_DIR, "app-version.json")
STATIC_DIR = os.path.join(SERVER_DIR, "static")


def _load_version():
    if not os.path.exists(VERSION_FILE):
        raise HTTPException(status_code=404, detail="未发布过应用版本")
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        log.exception("读取 app-version.json 失败")
        raise HTTPException(status_code=500, detail="版本信息损坏")
    for key in ("version_code", "version_name", "apk_url", "apk_file"):
        if key not in data:
            raise HTTPException(status_code=500, detail=f"版本信息缺字段 {key}")
    return data


@router.get("/api/v1/app-version")
def app_version():
    """返回最新安卓版本。客户端拿 version_code 和自己已装的比。"""
    data = _load_version()
    return {
        "version_code": data["version_code"],
        "version_name": data["version_name"],
        "apk_url": data["apk_url"],
        "changelog": data.get("changelog", ""),
        "apk_size": data.get("apk_size", 0),
        "sha256": data.get("sha256", ""),
    }


@router.get("/api/v1/app-download")
def app_download():
    """下载最新 APK。文件名来自 app-version.json 的 apk_file。"""
    data = _load_version()
    apk_file = data["apk_file"]
    # 只允许 static/ 下的文件，防止路径穿越。
    path = os.path.abspath(os.path.join(STATIC_DIR, apk_file))
    if not path.startswith(os.path.abspath(STATIC_DIR) + os.sep):
        raise HTTPException(status_code=400, detail="非法文件名")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="APK 文件不存在")
    return FileResponse(
        path,
        media_type="application/vnd.android.package-archive",
        filename=os.path.basename(path),
    )
