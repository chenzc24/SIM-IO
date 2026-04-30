procedure( build_symbol_lab()
    let((cv libName cellName bodyBBox bodyRect pinList pin)
        libName = "LLM_Layout_Design_Lab"
        cellName = "skill_lab"

        ; Open symbol cellview (create fresh with mode "w")
        cv = dbOpenCellViewByType(libName cellName "symbol" "schematicSymbol" "w")
        unless(cv
            error("Failed to open symbol cellview")
        )

        ; Draw symbol body rectangle
        bodyBBox = list(-0.625 -0.625 0.625 0.625)
        bodyRect = dbCreateRect(cv list("device" "drawing") bodyBBox)

        ; Define pins: name, direction, side, offset
        ; Left side pins (inputs)
        pinList = list(
            list("IN" "input" "left" 0.25)
            list("OUT" "output" "right" 0.25)
            list("VSS" "inputOutput" "bottom" 0.0)
        )

        ; Create each pin using leCreatePin
        foreach(pinInfo pinList
            let((pName pDir pSide pOffset pinXY accessDir)
                pName = car(pinInfo)
                pDir = cadr(pinInfo)
                pSide = caddr(pinInfo)
                pOffset = cadddr(pinInfo)

                ; Calculate pin position based on side
                if(pSide == "left" then
                    pinXY = list(-0.625 pOffset)
                    accessDir = "left"
                else if(pSide == "right" then
                    pinXY = list(0.625 pOffset)
                    accessDir = "right"
                else
                    pinXY = list(pOffset -0.625)
                    accessDir = "bottom"
                ))

                ; Create pin figure
                leCreatePin(cv list("device" "drawing") list(pinXY) pDir pName accessDir)
                printf("Created pin: %s (%s) at %L side=%s\n" pName pDir pinXY pSide)
            )
        )

        ; Add cell label
        dbCreateLabel(cv list("device" "drawing") list(0.0 0.0) cellName "centerCenter" "R0" "stick" 0.0625)

        ; Save
        dbSave(cv)
        printf("Symbol saved: %s/%s/symbol with %d figures\n" libName cellName length(cv~>figs))
        t
    )
)
build_symbol_lab()
