package com.testcorp.facade;

import com.testcorp.manager.Manager7;
import com.testcorp.util.StkGeneral;

public class Facade7 {
    private final Manager7 mgr = new Manager7();

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
