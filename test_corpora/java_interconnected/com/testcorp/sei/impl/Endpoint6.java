package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl6;
import com.testcorp.manager.Manager6;
import com.testcorp.facade.Facade6;
import com.testcorp.service.Service6;

@WebService
public class Endpoint6 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service6().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl6();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager6().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager6().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade6().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade6().orchestrateDirect(id);
    }

    @WebMethod
    public String viaLambda(String id) throws Exception {
        final Service6 s = new Service6();
        java.util.List<String> one = java.util.Collections.singletonList(id);
        final StringBuilder sb = new StringBuilder();
        one.forEach(v -> {
            try {
                sb.append(s.handle(v));
            } catch (Exception e) {
                sb.append("");
            }
        });
        return sb.toString();
    }

    @WebMethod
    public String viaAnon(final String id) throws Exception {
        Handler h = new Handler() {
            @Override
            public String run(String input) throws Exception {
                return new Service6().handleTraced(input);
            }
        };
        return h.run(id);
    }
}
