package com.testcorp.api;

import com.testcorp.service.Service11;

public class HandlerImpl11 implements Handler {
    private final Service11 svc = new Service11();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
