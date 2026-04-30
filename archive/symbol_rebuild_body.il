/*============================================================
 *  symbol_rebuild_body.il
 *  Delete old body rectangles and create new inner + outer.
 *
 *  Usage:
 *    symbolRebuildBody("lib" "cell"
 *        outerLeft outerRight outerBottom outerTop
 *        innerLeft innerRight innerBottom innerTop)
 *
 *  Returns: t on success.
 *============================================================*/

procedure( symbolRebuildBody(libName cellName
    oL oR oB oT iL iR iB iT)

    let((cv shapes s stype lname purp delCount)

        cv = dbOpenCellViewByType(libName cellName "symbol" "schematicSymbol" "a")
        unless(cv
            error("symbolRebuildBody: cannot open %s/%s/symbol\n" libName cellName)
        )

        ; ── Delete old body rects ──
        ; Outer body: rect on instance/drawing
        ; Inner body: rect on device/drawing
        ; We identify them by layer + purpose + objType
        shapes = setof(s cv~>shapes
            s~>objType == "rect" &&
            ( (s~>layerName == "instance" && s~>purpose == "drawing") ||
              (s~>layerName == "device"   && s~>purpose == "drawing") )
        )

        delCount = 0
        foreach(s shapes
            dbDeleteObject(s)
            delCount = delCount + 1
        )
        printf("REBUILD-BODY: deleted %d old rect(s)\n" delCount)

        ; ── Create new outer body (instance/drawing) ──
        dbCreateRect(cv list("instance" "drawing")
            list(list(oL oB) list(oR oT)))

        ; ── Create new inner body (device/drawing) ──
        dbCreateRect(cv list("device" "drawing")
            list(list(iL iB) list(iR iT)))

        dbSave(cv)
        printf("REBUILD-BODY: outer=(%g %g %g %g) inner=(%g %g %g %g)\n"
               oL oR oB oT iL iR iB iT)
        t
    )
)
