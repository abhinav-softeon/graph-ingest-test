package com.testcorp.api;

import com.testcorp.service.Service0;

public class HandlerImpl0 implements Handler {
    private final Service0 svc = new Service0();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
