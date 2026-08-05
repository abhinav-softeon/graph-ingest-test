package com.testcorp.facade;

import com.testcorp.manager.Manager0;
import com.testcorp.util.StkGeneral;

public class Facade0 {
    private final Manager0 mgr = new Manager0();

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
