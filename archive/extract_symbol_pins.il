/*============================================================
 *  extract_symbol_pins.il
 *  Extract all pin info from a TSG-generated symbol cellview.
 *  Returns: list of (pinName direction side) tuples
 *  Side is determined by pin position relative to symbol center.
 *============================================================*/
procedure( extract_symbol_pins(libName cellName)
    let((cv terminals result symBBox cx cy)
        cv = dbOpenCellViewByType(libName cellName "symbol" nil "r")
        unless(cv
            printf("ERROR: Cannot open %s/%s/symbol\n" libName cellName)
            return(nil)
        )

        symBBox = cv~>bBox
        cx = (car(car(symBBox)) + car(cadr(symBBox))) / 2.0
        cy = (cadr(car(symBBox)) + cadr(cadr(symBBox))) / 2.0

        result = nil
        foreach(term cv~>terminals
            let((tName tDir pins fig pinBBox px py side)
                tName = term~>name
                tDir = term~>direction
                pins = term~>pins
                when(pins
                    fig = car(car(pins)~>figs)
                    when(fig
                        pinBBox = fig~>bBox
                        px = (car(car(pinBBox)) + car(cadr(pinBBox))) / 2.0
                        py = (cadr(car(pinBBox)) + cadr(cadr(pinBBox))) / 2.0

                        if(px < cx - 0.1 then
                            side = "left"
                        else if(px > cx + 0.1 then
                            side = "right"
                        else if(py > cy + 0.1 then
                            side = "top"
                        else
                            side = "bottom"
                        ))

                        result = cons(list(tName tDir side) result)
                    )
                )
            )
        )
        reverse(result)
    )
)
