package com.testcorp.api;

import com.testcorp.service.Service10;

public class HandlerImpl10 implements Handler {
    private final Service10 svc = new Service10();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
