package com.testcorp.facade;

import com.testcorp.manager.Manager5;
import com.testcorp.util.StkGeneral;

public class Facade5 {
    private final Manager5 mgr = new Manager5();

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
