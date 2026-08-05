package com.testcorp.service;

import com.testcorp.dao.Dao9;
import com.testcorp.util.StkGeneral;

public class Service9 {
    private final Dao9 dao = new Dao9();

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
        return new Service10().handle(id);
    }
}
