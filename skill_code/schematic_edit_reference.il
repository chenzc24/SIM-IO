/*============================================================
 *  Schematic Edit Toolkit — verified on LLM_Layout_Design_Lab
 *  Date: 2026-04-30
 *
 *  所有操作均以 edit (append) 模式打开 cellview，
 *  不会清除已有内容。仅 mode="w" 用于首次创建。
 *============================================================*/

/*------------------------------------------------------------
 * 1. 打开 cellview（edit 模式，保留已有内容）
 *------------------------------------------------------------
 * SKILL:
 *   cv = dbOpenCellViewByType("lib" "cell" "schematic" "schematic" "a")
 *
 * 注意:
 *   - mode "a" = append/edit（保留内容，不存在则创建）
 *   - mode "w" = write/overwrite（清空重建，首次创建时用）
 *   - mode "r" = read-only
 *
 * Python bridge:
 *   open_cell_view_by_type(lib, cell, view="schematic", mode="a")
 *   # ⚠️ client.py 默认 mode="w"，必须显式传 mode="a"
 */

/*------------------------------------------------------------
 * 2. 放置 instance
 *------------------------------------------------------------
 * SKILL:
 *   dbCreateInst(cv masterCellView instName xy orientation)
 *
 *   masterCellView 获取方式:
 *     dbOpenCellViewByType("lib" "cell" "symbol")
 *     ⚠️ 不要加第4个参数 viewType!
 *        ✗ dbOpenCellViewByType("analogLib" "vdc" "symbol" "symbol" "r")  → nil
 *        ✓ dbOpenCellViewByType("analogLib" "vdc" "symbol")               → OK
 *
 *   orientation: "R0" "R90" "R180" "R270" "MX" "MY" ...
 *
 * 示例:
 *   cv = dbOpenCellViewByType("LLM_Layout_Design_Lab" "skill_lab" "schematic" "schematic" "a")
 *   dbCreateInst(cv dbOpenCellViewByType("analogLib" "vdc" "symbol") "V0" list(0.0 0.0) "R0")
 *   dbCreateInst(cv dbOpenCellViewByType("analogLib" "vpulse" "symbol") "VP0" list(-4.0 0.0) "R0")
 *   dbCreateInst(cv dbOpenCellViewByType("analogLib" "cap" "symbol") "C0" list(4.0 0.0) "R0")
 *   dbCreateInst(cv dbOpenCellViewByType("analogLib" "nmos4" "symbol") "MN0" list(0.0 -2.0) "R0")
 *
 * 已验证 analogLib 常用 cell:
 *   vdc, vpulse, cap, res, nmos4, pmos4, vsin, vpwl, idc, ipulse
 *   vdd, vss, gnd（供电符号）
 */

/*------------------------------------------------------------
 * 3. 设置 instance CDF 参数
 *------------------------------------------------------------
 * SKILL:
 *   car(setof(p cdfGetInstCDF(inst)~>parameters p~>name=="paramName"))~>value = "paramValue"
 *
 * 查询参数列表:
 *   cdfGetInstCDF(inst)~>parameters~>name
 *
 * 查询当前值:
 *   car(setof(p cdfGetInstCDF(inst)~>parameters p~>name=="vdc"))~>value
 *
 * 获取 instance 对象:
 *   car(setof(x cv~>instances x~>name=="V0"))
 *
 * VDC 常用参数:  vdc, acm, acp, xfm, pacm, pacp, tc1, tc2, tnom, srcType
 * VPULSE 常用参数: v1, v2, period, rise, fall, width, delay, duty
 * CAP 常用参数:   c, m, tc1, tc2, tnom
 * NMOS4/PMOS4:   w, l, m, model
 *
 * 示例 (完整命令):
 *   cv = dbOpenCellViewByType("LLM_Layout_Design_Lab" "skill_lab" "schematic" "schematic" "a")
 *   inst = car(setof(x cv~>instances x~>name=="V0"))
 *   car(setof(p cdfGetInstCDF(inst)~>parameters p~>name=="vdc"))~>value = "1.8"
 *
 *   instVP = car(setof(x cv~>instances x~>name=="VP0"))
 *   car(setof(p cdfGetInstCDF(instVP)~>parameters p~>name=="v1"))~>value = "0"
 *   car(setof(p cdfGetInstCDF(instVP)~>parameters p~>name=="v2"))~>value = "1.8"
 *   car(setof(p cdfGetInstCDF(instVP)~>parameters p~>name=="period"))~>value = "100n"
 *   car(setof(p cdfGetInstCDF(instVP)~>parameters p~>name=="rise"))~>value = "1n"
 *   car(setof(p cdfGetInstCDF(instVP)~>parameters p~>name=="fall"))~>value = "1n"
 *   car(setof(p cdfGetInstCDF(instVP)~>parameters p~>name=="width"))~>value = "50n"
 */

/*------------------------------------------------------------
 * 4. 创建 net
 *------------------------------------------------------------
 * SKILL:
 *   dbCreateNet(cv "netName")
 *
 * 示例:
 *   dbCreateNet(cv "IN")
 *   dbCreateNet(cv "OUT")
 *   dbCreateNet(cv "VDD")
 *   dbCreateNet(cv "VSS")
 */

/*------------------------------------------------------------
 * 5. 创建 wire（连线）
 *------------------------------------------------------------
 * SKILL (9 个参数，已验证):
 *   schCreateWire(cv "route" "full" list(list(x1 y1) list(x2 y2)) 0 0 0 nil nil)
 *
 *   也可画折线:
 *   schCreateWire(cv "route" "full" list(list(x1 y1) list(x2 y2) list(x3 y3)) 0 0 0 nil nil)
 *
 * 示例:
 *   schCreateWire(cv "route" "full" list(list(-2.5 0.5) list(0.0 0.5)) 0 0 0 nil nil)
 *   schCreateWire(cv "route" "full" list(list(-2.5 -0.5) list(-2.5 -2.0) list(0.0 -2.0)) 0 0 0 nil nil)
 */

/*------------------------------------------------------------
 * 6. 创建 wire label（网络标签）
 *------------------------------------------------------------
 * SKILL:
 *   schCreateWireLabel(cv nil list(x y) "labelText" "align" "rotation" "stick" 0.0625 nil)
 *
 *   align: "centerLeft" "centerRight" "centerCenter" ...
 *   rotation: "0" "90" "180" "270"
 *
 * 示例:
 *   schCreateWireLabel(cv nil list(-1.0 0.5) "IN" "centerLeft" "0" "stick" 0.0625 nil)
 *   schCreateWireLabel(cv nil list(1.5 0.5) "OUT" "centerLeft" "0" "stick" 0.0625 nil)
 */

/*------------------------------------------------------------
 * 7. 创建 pin
 *------------------------------------------------------------
 * SKILL:
 *   schCreatePin(cv nil "pinName" "direction" nil list(x y) "orientation")
 *
 *   direction: "input" "output" "inputOutput"
 *   orientation: "left" "right" "top" "bottom"
 *
 * 示例:
 *   schCreatePin(cv nil "IN" "input" nil list(-2.5 0.5) "left")
 *   schCreatePin(cv nil "OUT" "output" nil list(4.0 0.5) "right")
 *   schCreatePin(cv nil "VSS" "inputOutput" nil list(2.0 -2.0) "bottom")
 */

/*------------------------------------------------------------
 * 8. 保存 cellview
 *------------------------------------------------------------
 * SKILL:
 *   dbSave(cv)
 */

/*------------------------------------------------------------
 * 9. 查询操作
 *------------------------------------------------------------
 * 查询 instance 列表:
 *   cv~>instances~>name
 *
 * 查询 instance 坐标:
 *   car(setof(x cv~>instances x~>name=="V0"))~>xy
 *
 * 查询 instance 的 terminal:
 *   car(setof(x cv~>instances x~>name=="V0"))~>instTerms~>term~>name
 *
 * 查询 net 列表:
 *   cv~>nets~>name
 *
 * 查询 analogLib cell 列表:
 *   ddGetObj("analogLib")~>cells~>name
 *
 * 查询某个 cell 的 view 列表:
 *   ddGetObj("analogLib" "vdc")~>views~>name
 *
 * 查询 instance CDF 参数名:
 *   cdfGetInstCV(car(setof(x cv~>instances x~>name=="V0")))~>parameters~>name
 *
 * 查询 library 列表:
 *   ddGetLibList()~>name
 */
