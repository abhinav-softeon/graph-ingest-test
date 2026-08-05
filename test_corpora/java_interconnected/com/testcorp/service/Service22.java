package com.testcorp.service;

import com.testcorp.dao.Dao22;
import com.testcorp.util.StkGeneral;

public class Service22 {
    private final Dao22 dao = new Dao22();

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
        return new Service23().handle(id);
    }
}
