package com.testcorp.facade;

import com.testcorp.manager.Manager2;
import com.testcorp.util.StkGeneral;

public class Facade2 {
    private final Manager2 mgr = new Manager2();

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
