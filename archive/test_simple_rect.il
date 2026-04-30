procedure( test_simple_rect()
    let((cv rect)
        cv = dbOpenCellViewByType("LLM_Layout_Design_Lab" "skill_lab" "symbol" "schematicSymbol" "w")
        if(cv then
            printf("Opened symbol cv\n")
            rect = dbCreateRect(cv list("device" "drawing") list(-0.5 -0.5 0.5 0.5))
            if(rect then
                printf("Rect OK\n")
            else
                printf("Rect FAILED\n")
            )
            dbSave(cv)
            printf("Figs: %d\n" length(cv~>figs))
        else
            printf("Failed to open\n")
        )
        t
    )
)
test_simple_rect()
