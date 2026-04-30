procedure( build_skill_lab_symbol()
    let((cv)
        cv = dbOpenCellViewByType("LLM_Layout_Design_Lab" "skill_lab" "symbol" "schematicSymbol" "w")
        unless(cv
            error("Failed to open symbol cv")
        )

        ; 1. Draw symbol body rectangle
        dbCreateRect(cv list("instance" "drawing") list(list(-0.625 -0.5) list(0.625 0.5)))

        ; 2. Create pins using leCreatePin
        ;    leCreatePin(cv lpp points direction name accessDir)
        ; IN - input, left side
        leCreatePin(cv list("instance" "drawing") list(list(-0.625 0.0)) "input" "IN" "left")

        ; OUT - output, right side
        leCreatePin(cv list("instance" "drawing") list(list(0.625 0.0)) "output" "OUT" "right")

        ; VSS - inputOutput, bottom
        leCreatePin(cv list("instance" "drawing") list(list(0.0 -0.5)) "inputOutput" "VSS" "bottom")

        ; 3. Add cell name label
        dbCreateLabel(cv list("instance" "drawing") list(0.0 0.0) "skill_lab" "centerCenter" "R0" "stick" 0.0625)

        ; 4. Save
        dbSave(cv)
        printf("Symbol built: %d shapes\n" length(cv~>shapes))
        t
    )
)
build_skill_lab_symbol()
