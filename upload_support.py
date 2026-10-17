"""Validate model fields and transfer the raw CSV via a short-lived private URL."""
import base64
import csv
import hashlib
import io
import json
import math
import re
import time
import hmac
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

FIELDS = (
    "age", "gender", "temperature", "pulse", "systolic", "diastolic",
    "left_naked_eye", "right_naked_eye", "heart_rate", "hear", "exercise", "heart_rhythm",
    "smoke", "HGB", "PLT", "Creatinine", "Urea", "TC", "LDL_C", "HDL_C",
    "hypertension_final", "diabetes_final", "dyslipidemia_final",
    "height_cm", "waist_cm", "weight_kg", "glucose_mmolL", "triglycerides_mmolL",
)
BINARY = {"gender", "hear", "exercise", "heart_rhythm", "smoke", "hypertension_final", "diabetes_final", "dyslipidemia_final"}
POSITIVE = {"height_cm", "waist_cm", "weight_kg", "glucose_mmolL", "triglycerides_mmolL"}
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10000


class InputError(ValueError):
    pass


def sheet_names(raw):
    _check_xlsx(raw)
    import openpyxl
    book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
    try:
        return list(book.sheetnames)
    finally:
        book.close()


def _check_xlsx(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise InputError("文件为空或超过5MiB。")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024:
                raise InputError("Excel解压后超过32MiB，请另存为CSV。")
    except zipfile.BadZipFile:
        raise InputError("这不是有效的xlsx文件，请使用Excel另存为xlsx。") from None


def prepare_table(raw, suffix, sheet=None):
    """Validate numbers and send only the 28 model fields, preserving row order."""
    if not raw or len(raw) > MAX_BYTES:
        raise InputError("文件为空或超过5MiB。")
    book = None
    if suffix == ".csv":
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        else:
            raise InputError("CSV编码无法识别，请另存为UTF-8 CSV。")
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    elif suffix == ".xlsx":
        _check_xlsx(raw)
        import openpyxl
        book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
        if sheet not in book.sheetnames:
            book.close()
            raise InputError("请选择要评估的Excel工作表。")
        reader = book[sheet].iter_rows(values_only=True)
    else:
        raise InputError("仅支持csv和xlsx；旧xls请先另存为xlsx。")
    try:
        header = next(reader, None)
        if header is None:
            raise InputError("表格为空。")
        names = [str(value).strip() if value is not None else "" for value in header]
        if any(names.count(field) > 1 for field in FIELDS):
            raise InputError("模型字段列名重复，请检查表头。")
        missing = [field for field in FIELDS if field not in names]
        if missing:
            raise InputError("缺少列：" + "、".join(missing))
        indices = [names.index(field) for field in FIELDS]
        out = io.StringIO(newline="")
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(FIELDS)
        count = 0
        for row in reader:
            count += 1
            if count > MAX_ROWS:
                raise InputError("最多支持10000条，请拆分文件。")
            values = []
            for field, index in zip(FIELDS, indices):
                cell = row[index] if index < len(row) else None
                try:
                    if isinstance(cell, bool) or cell is None or str(cell).strip() == "":
                        raise ValueError
                    value = float(cell)
                    if not math.isfinite(value) or (field in BINARY and value not in (0, 1)) or (field in POSITIVE and value <= 0):
                        raise ValueError
                    if field == "age" and value < 65:
                        raise ValueError
                except (ValueError, TypeError, OverflowError):
                    raise InputError(f"第{count}条数据的{field}缺失或不符合模型要求，请检查原表。") from None
                values.append(format(value, ".17g"))
            writer.writerow(values)
        if count == 0:
            raise InputError("表格只有表头，没有数据。")
        payload = out.getvalue().encode("utf-8")
        if len(payload) > MAX_BYTES:
            raise InputError("转换后的CSV超过5MiB，请拆分文件。")
        return payload, count
    except csv.Error:
        raise InputError("CSV结构不正确，请检查分隔符和引号。") from None
    finally:
        if book is not None:
            book.close()


def safe_tag(value):
    value = str(value or "unknown")
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else "unknown"


class UploadError(RuntimeError):
    def __init__(self, stage, code="unknown", request_id=None):
        super().__init__(f"{stage}未完成；code={safe_tag(code)}；request_id={safe_tag(request_id)}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SignedFileClient:
    def __init__(self, config, bucket_factory=None, opener=None):
        names = ("OSS_BUCKET", "OSS_ENDPOINT", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET")
        if any(not config.get(name) for name in names):
            raise InputError("请管理员补齐Secrets中的OSS_BUCKET、OSS_ENDPOINT、OSS_ACCESS_KEY_ID和OSS_ACCESS_KEY_SECRET。")
        bucket = config["OSS_BUCKET"]
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket) is None:
            raise InputError("OSS_BUCKET名称格式不正确。")
        if config["OSS_ENDPOINT"].rstrip("/") != "https://oss-cn-beijing.aliyuncs.com":
            raise InputError("本修复版使用北京OSS，请填写https://oss-cn-beijing.aliyuncs.com。")
        if bucket_factory is None:
            import oss2
            bucket_factory = lambda: oss2.Bucket(oss2.Auth(config["OSS_ACCESS_KEY_ID"], config["OSS_ACCESS_KEY_SECRET"]), config["OSS_ENDPOINT"], bucket)
        self.bucket_factory = bucket_factory
        self.host = bucket + ".oss-cn-beijing.aliyuncs.com"
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def upload(self, data, count):
        if not 1 <= len(data) <= MAX_BYTES or type(count) is not int or not 1 <= count <= MAX_ROWS:
            raise InputError("输入大小或记录数不正确。")
        digest = hashlib.sha256(data).hexdigest()
        key = "cmm-input/" + uuid.uuid4().hex + ".csv"
        try:
            bucket = self.bucket_factory()
            bucket.put_object(key, data, headers={"Content-Type": "text/csv; charset=utf-8"})
            url = bucket.sign_url("GET", key, 900, slash_safe=True)
        except Exception as exc:
            raise UploadError("上传私有文件", getattr(exc, "code", "UploadFailed"), getattr(exc, "request_id", None)) from None
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != self.host or parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise UploadError("检查下载链接", "UnexpectedDestination")
        try:
            request = urllib.request.Request(url, method="GET", headers={"Accept-Encoding": "identity"})
            with self.opener.open(request, timeout=45) as response:
                if response.status != 200:
                    raise UploadError("核对下载文件", "UnexpectedStatus")
                downloaded = response.read(MAX_BYTES + 1)
        except UploadError:
            raise
        except Exception:
            raise UploadError("核对下载链接", "DownloadFailed") from None
        if len(downloaded) != len(data) or not hmac.compare_digest(hashlib.sha256(downloaded).hexdigest(), digest):
            raise UploadError("核对文件完整性", "HashOrSizeMismatch")
        return {"url": url, "sha256": digest, "bytes": len(data), "records": count}


def link_expired(manifest, now=None):
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(manifest["url"]).query)
        return int(query["Expires"][0]) <= int(time.time() if now is None else now) + 30
    except (KeyError, ValueError, TypeError, IndexError):
        return True


def conversation_body(prompt, session_id=None, manifest=None):
    input_data = {"prompt": prompt}
    if session_id:
        input_data["session_id"] = session_id
    if manifest:
        if link_expired(manifest):
            raise InputError("文件链接已过期或即将过期，请重新点击准备并上传。")
        input_data["file_list"] = [manifest["url"]]
        input_data["prompt"] += (
            "\n\n本次CMM计算的cmm_transfer_manifest如下，仅为本次私有文件传输清单，不含体检表内容：\n"
            + json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
            + "\n请调用cmm-risk-assessment的v1.3受限链接入口：在技能目录执行python3 scripts/fetch_and_run.py，"
            "将上面四字段JSON通过引号保护的heredoc送入标准输入。不要寻找file_session或假定原始附件已挂载。"
            "不要扫描系统目录、遍历环境变量、打印完整体检表或结果；不得将链接、清单或个人数据发送到Neo4j MCP。"
            "不要修改CMM_FILE_HOST或跳过校验。只有脚本确实返回completed才报告计算完成；"
            "如果报错，原样报告固定错误码，不凭解析文本重建表格或猜测结果。"
            "计算成功后，使用宿主已授权的download_file仅导出该任务返回的result.csv，并提供真实下载链接。"
        )
    return {"input": input_data, "parameters": {}}

