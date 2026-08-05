package com.testcorp.facade;

import com.testcorp.manager.Manager6;
import com.testcorp.util.StkGeneral;

public class Facade6 {
    private final Manager6 mgr = new Manager6();

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
