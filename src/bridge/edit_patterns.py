"""
Bridge 调用模式速查 — Python 端通过 rb_exec 操作 Virtuoso

关键发现:
  1. rb_exec() 包装命令为 let 块，只支持单表达式返回值
     ✗ cv = dbOpenCellViewByType(...); cv~>libName   → 语法错误
     ✓ dbOpenCellViewByType(...)~>libName              → 单表达式 OK

  2. dbOpenCellViewByType 打开 analogLib symbol 时不要加 viewType
     ✗ dbOpenCellViewByType("analogLib" "vdc" "symbol" "symbol" "r")  → 返回 nil
     ✓ dbOpenCellViewByType("analogLib" "vdc" "symbol")               → OK

  3. mode 参数:
     "a" = append/edit（保留内容，不存在则创建）
     "w" = write/overwrite（清空重建）
     "r" = read-only
     ⚠️ client.py 默认 mode="w"，编辑已有 cellview 必须传 mode="a"

  4. 复杂多步操作应写成 .il 文件，用 load_skill_file() 加载
     单行表达式可用 rb_exec() 或 client.execute_skill()
"""

from pathlib import Path
import sys

# src/bridge/ → bridge-Agent/io-ring-orchestrator-T28/
_T28_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "io-ring-orchestrator-T28"
if str(_T28_ROOT) not in sys.path:
    sys.path.insert(0, str(_T28_ROOT))

from io_ring.bridge import rb_exec, load_skill_file, open_cell_view_by_type, ge_open_window, save_current_cellview, ui_redraw
from io_ring.bridge.client import _get_client


def get_cv_info(lib: str, cell: str, view: str = "schematic") -> dict:
    """获取 cellview 基本信息"""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "{view}" "schematic" "a")'
    return {
        "lib": rb_exec(f'{cv}~>libName', timeout=15).strip().strip('"'),
        "cell": rb_exec(f'{cv}~>cellName', timeout=15).strip().strip('"'),
        "instances": rb_exec(f'length({cv}~>instances)', timeout=15).strip(),
        "nets": rb_exec(f'{cv}~>nets~>name', timeout=15).strip(),
    }


def place_instance(lib: str, cell: str, master_lib: str, master_cell: str,
                   inst_name: str, x: float, y: float, orient: str = "R0") -> str:
    """放置 instance（不指定 viewType）"""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    master = f'dbOpenCellViewByType("{master_lib}" "{master_cell}" "symbol")'
    result = rb_exec(f'dbCreateInst({cv} {master} "{inst_name}" list({x} {y}) "{orient}")~>name', timeout=30)
    return result.strip().strip('"')


def set_cdf_param(lib: str, cell: str, inst_name: str,
                  param_name: str, param_value: str) -> str:
    """设置 instance CDF 参数"""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    inst = f'car(setof(x {cv}~>instances x~>name=="{inst_name}"))'
    param = f'car(setof(p cdfGetInstCDF({inst})~>parameters p~>name=="{param_name}"))'
    result = rb_exec(f'{param}~>value = "{param_value}"', timeout=15)
    return result.strip()


def create_wire(lib: str, cell: str, points: list, ) -> str:
    """创建连线 points = [(x1,y1), (x2,y2), ...]"""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    pts_str = " ".join(f"list({x} {y})" for x, y in points)
    result = rb_exec(f'schCreateWire({cv} "route" "full" list({pts_str}) 0 0 0 nil nil)', timeout=15)
    return result.strip()


def create_wire_label(lib: str, cell: str, x: float, y: float,
                      text: str, align: str = "centerLeft") -> str:
    """创建网络标签"""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    result = rb_exec(f'schCreateWireLabel({cv} nil list({x} {y}) "{text}" "{align}" "0" "stick" 0.0625 nil)', timeout=15)
    return result.strip()


def create_pin(lib: str, cell: str, name: str, direction: str,
               x: float, y: float, orient: str = "left") -> str:
    """创建 pin"""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    result = rb_exec(f'schCreatePin({cv} nil "{name}" "{direction}" nil list({x} {y}) "{orient}")', timeout=15)
    return result.strip()


def create_net(lib: str, cell: str, net_name: str) -> str:
    """创建 net"""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "a")'
    result = rb_exec(f'dbCreateNet({cv} "{net_name}")~>name', timeout=15)
    return result.strip().strip('"')


def save_cv(lib: str, cell: str, view: str = "schematic") -> str:
    """保存 cellview"""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "{view}" "schematic" "a")'
    result = rb_exec(f'dbSave({cv})', timeout=15)
    return result.strip()


# ====== Symbol 视图操作 ======

def create_symbol_rect(lib: str, cell: str,
                       x1: float, y1: float, x2: float, y2: float,
                       layer: str = "instance", purpose: str = "drawing") -> str:
    """在 symbol 视图中画矩形（bBox 格式: ((x1 y1) (x2 y2))）"""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "symbol" "schematicSymbol" "a")'
    result = rb_exec(
        f'dbCreateRect({cv} list("{layer}" "{purpose}") '
        f'list(list({x1} {y1}) list({x2} {y2})))~>objType', timeout=15)
    return result.strip()


def create_symbol_label(lib: str, cell: str, x: float, y: float,
                        text: str, align: str = "centerCenter",
                        layer: str = "device", purpose: str = "drawing") -> str:
    """在 symbol 视图中添加标签"""
    cv = f'dbOpenCellViewByType("{lib}" "{cell}" "symbol" "schematicSymbol" "a")'
    result = rb_exec(
        f'dbCreateLabel({cv} list("{layer}" "{purpose}") '
        f'list({x} {y}) "{text}" "{align}" "R0" "stick" 0.0625)', timeout=15)
    return result.strip()


def screenshot(local_path: str, remote_path: str = None) -> str:
    """截图并下载到本地"""
    import time
    client = _get_client()
    if remote_path is None:
        remote_path = "/tmp/vb_screenshot.png"
    load_skill_file(str(_T28_ROOT / "skill_code" / "screenshot.il"))
    client.execute_skill(f'takeScreenshot("{remote_path}")', timeout=30)
    time.sleep(1)
    from io_ring.bridge.client import _get_ssh
    ssh = _get_ssh()
    ssh.download_file(remote_path, Path(local_path))
    return local_path
