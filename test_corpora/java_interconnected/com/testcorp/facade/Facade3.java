package com.testcorp.facade;

import com.testcorp.manager.Manager3;
import com.testcorp.util.StkGeneral;

public class Facade3 {
    private final Manager3 mgr = new Manager3();

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
