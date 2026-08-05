package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl6;
import com.testcorp.manager.Manager8;
import com.testcorp.facade.Facade2;
import com.testcorp.service.Service18;

@WebService
public class Endpoint18 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service18().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl6();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager8().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager8().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade2().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade2().orchestrateDirect(id);
    }

    @WebMethod
    public String viaLambda(String id) throws Exception {
        final Service18 s = new Service18();
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
                return new Service18().handleTraced(input);
            }
        };
        return h.run(id);
    }
}
