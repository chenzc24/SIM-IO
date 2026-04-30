procedure( test_symbol_rect()
    let((cv rect layerList)
        cv = dbOpenCellViewByType("LLM_Layout_Design_Lab" "skill_lab" "symbol" "schematicSymbol" "w")
        unless(cv
            error("Failed to open symbol cv")
        )
        printf("Opened symbol cv: %s\n" cv~>cellName)

        ; Check available layers
        layerList = cv~>cell~>views
        printf("Views: %L\n" layerList)

        ; Try to get tech file layers
        let((techFile)
            techFile = techGetTechFile(cv)
            if(techFile then
                printf("Tech file layers: %L\n" techFile~>layerRules~>layers~>name)
            else
                printf("No tech file\n")
            )
        )

        ; Try creating rectangle with different layer names
        rect = dbCreateRect(cv list("device" "drawing") list(-0.5 -0.5 0.5 0.5))
        if(rect then
            printf("Rect created OK: %L\n" rect)
        else
            printf("Rect FAILED with device/drawing\n")
            ; Try wire drawing layer
            rect = dbCreateRect(cv list("wire" "drawing") list(-0.5 -0.5 0.5 0.5))
            if(rect then
                printf("Rect created OK with wire/drawing: %L\n" rect)
            else
                printf("Rect FAILED with wire/drawing too\n")
            )
        )

        dbSave(cv)
        printf("Saved. Figs count: %d\n" length(cv~>figs))
        t
    )
)
test_symbol_rect()
