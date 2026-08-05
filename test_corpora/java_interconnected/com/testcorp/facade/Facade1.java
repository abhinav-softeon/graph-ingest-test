package com.testcorp.facade;

import com.testcorp.manager.Manager1;
import com.testcorp.util.StkGeneral;

public class Facade1 {
    private final Manager1 mgr = new Manager1();

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
