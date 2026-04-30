procedure( build_skill_lab_symbol()
    let((cv bodyRect pinFig pinObj term label)
        cv = dbOpenCellViewByType("LLM_Layout_Design_Lab" "skill_lab" "symbol" "schematicSymbol" "w")
        unless(cv
            error("Failed to open symbol cv")
        )

        ; 1. Body rectangle on instance/drawing
        dbCreateRect(cv list("instance" "drawing") list(list(-0.625 -0.5) list(0.625 0.5)))

        ; 2. Build each pin: draw pin rect -> create term -> create pin
        ; IN - input, left side
        pinFig = dbCreateRect(cv list("pin" "drawing") list(list(-0.65 -0.025) list(-0.6 0.025)))
        term = dbCreateTerm(cv "IN" "input")
        pinObj = dbCreatePin(term pinFig)
        if(pinObj then printf("Pin IN OK\n") else printf("Pin IN FAILED\n"))

        ; OUT - output, right side
        pinFig = dbCreateRect(cv list("pin" "drawing") list(list(0.6 -0.025) list(0.65 0.025)))
        term = dbCreateTerm(cv "OUT" "output")
        pinObj = dbCreatePin(term pinFig)
        if(pinObj then printf("Pin OUT OK\n") else printf("Pin OUT FAILED\n"))

        ; VSS - inputOutput, bottom
        pinFig = dbCreateRect(cv list("pin" "drawing") list(list(-0.025 -0.55) list(0.025 -0.5)))
        term = dbCreateTerm(cv "VSS" "inputOutput")
        pinObj = dbCreatePin(term pinFig)
        if(pinObj then printf("Pin VSS OK\n") else printf("Pin VSS FAILED\n"))

        ; 3. Pin name labels on device/drawing layer
        dbCreateLabel(cv list("device" "drawing") list(-0.7 0.0) "IN" "centerRight" "R0" "stick" 0.0625)
        dbCreateLabel(cv list("device" "drawing") list(0.7 0.0) "OUT" "centerLeft" "R0" "stick" 0.0625)
        dbCreateLabel(cv list("device" "drawing") list(0.0 -0.6) "VSS" "centerTop" "R0" "stick" 0.0625)

        ; 4. Cell name label
        dbCreateLabel(cv list("device" "drawing") list(0.0 0.0) "skill_lab" "centerCenter" "R0" "stick" 0.0625)

        ; 5. Save
        dbSave(cv)
        printf("Symbol built: %d shapes, %d terminals\n" length(cv~>shapes) length(cv~>terminals))
        t
    )
)
build_skill_lab_symbol()
