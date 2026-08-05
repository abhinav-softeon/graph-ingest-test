package com.testcorp.service;

import com.testcorp.dao.Dao15;
import com.testcorp.util.StkGeneral;

public class Service15 {
    private final Dao15 dao = new Dao15();

    public String handle(String id) throws Exception {
        if (StkGeneral.isEmpty(id)) {
            return "";
        }
        String out = dao.load(id);
        return out;
    }

    public String handleTraced(String id) throws Exception {
        return dao.load(id, true);
    }

    public String viaPeer(String id) throws Exception {
        return new Service16().handle(id);
    }
}
