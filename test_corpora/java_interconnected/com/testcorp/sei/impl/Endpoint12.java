package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl0;
import com.testcorp.manager.Manager2;
import com.testcorp.facade.Facade4;
import com.testcorp.service.Service12;

@WebService
public class Endpoint12 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service12().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl0();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager2().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager2().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade4().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade4().orchestrateDirect(id);
    }

    @WebMethod
    public String viaLambda(String id) throws Exception {
        final Service12 s = new Service12();
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
                return new Service12().handleTraced(input);
            }
        };
        return h.run(id);
    }
}
