package com.testcorp.facade;

import com.testcorp.manager.Manager4;
import com.testcorp.util.StkGeneral;

public class Facade4 {
    private final Manager4 mgr = new Manager4();

    public String orchestrate(String id) throws Exception {
        if (StkGeneral.isEmpty(id)) {
            return "";
        }
        return mgr.chain(id);
    }

    public String orchestrateDirect(String id) throws Exception {
        return mgr.process(id);
    }
}
