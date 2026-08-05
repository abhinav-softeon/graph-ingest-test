package com.testcorp.api;

import com.testcorp.service.Service4;

public class HandlerImpl4 implements Handler {
    private final Service4 svc = new Service4();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
