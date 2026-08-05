package com.testcorp.api;

import com.testcorp.service.Service9;

public class HandlerImpl9 implements Handler {
    private final Service9 svc = new Service9();

    @Override
    public String run(String input) throws Exception {
        return svc.handle(input);
    }
}
